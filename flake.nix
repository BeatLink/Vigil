{
    description = "A lightweight, pluggable network and system monitor for Linux";

    inputs = {
        nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
        flake-utils.url = "github:numtide/flake-utils";
    };

    outputs =
        {
            self,
            nixpkgs,
            flake-utils,
        }:
        flake-utils.lib.eachDefaultSystem (
            system:
            let
                # inline-snapshot 0.32.5 fails its own documentation tests in
                # current nixpkgs and is not cached by Hydra, which breaks the
                # build of anything pulling it in as a check dependency (pydantic
                # → FastAPI → NiceGUI, in our case). Disable its test suite so
                # the package builds from source without running those tests.
                inlineSnapshotFix = final: prev: {
                    python312 = prev.python312.override {
                        packageOverrides = pyfinal: pyprev: {
                            inline-snapshot = pyprev.inline-snapshot.overridePythonAttrs (old: {
                                doCheck = false;
                                pytestCheckPhase = "true";
                            });
                        };
                    };
                    python312Packages = final.python312.pkgs;
                };
                pkgs = import nixpkgs {
                    inherit system;
                    overlays = [ inlineSnapshotFix ];
                };
                pyproject = builtins.fromTOML (builtins.readFile ./pyproject.toml);

                pythonDeps = with pkgs.python312Packages; [
                    requests
                    pyyaml
                    peewee
                    nicegui
                    dnspython
                    # Native async SSH transport (see
                    # vigil/core/connectors/ssh_connector.py) — the agentless
                    # way of reaching a monitored host.
                    asyncssh
                    # The agent transport's client half (vigil_agent/client.py).
                    # The server side needs nothing extra: nicegui already
                    # brings FastAPI/uvicorn, which serve the agent WebSocket.
                    websockets
                ];

                vigil-pkg = pkgs.python312Packages.buildPythonApplication {
                    pname = pyproject.project.name;
                    version = pyproject.project.version;
                    format = "pyproject";
                    src = ./.;

                    nativeBuildInputs = [ pkgs.python312Packages.setuptools ];
                    propagatedBuildInputs = pythonDeps;

                    pythonImportsCheck = [ "vigil" ];
                };

                # The agent, packaged apart from the server.
                #
                # A monitored host installs only this, so it must not pull in
                # nicegui, peewee, dnspython or asyncssh — none of which the
                # agent imports, and building them on every target is both
                # wasteful and, on aarch64, a source of build failures the
                # monitored host has no reason to care about.
                #
                # `vigil_agent` imports nothing from `vigil` (the wire protocol
                # lives in vigil_agent/protocol.py precisely so it doesn't), so
                # copying that one package in is the whole install.
                agentPython = pkgs.python312.withPackages (ps: with ps; [
                    websockets
                    pyyaml
                ]);

                agentSrc = pkgs.runCommand "vigil-agent-src" { } ''
                    mkdir -p $out
                    cp -r ${./vigil_agent} $out/vigil_agent
                '';

                vigil-agent-pkg = pkgs.writeShellApplication {
                    name = "vigil-agent";
                    text = ''
                        export PYTHONPATH="${agentSrc}''${PYTHONPATH:+:$PYTHONPATH}"
                        exec ${agentPython}/bin/python3 -m vigil_agent "$@"
                    '';
                };

                # Vigil runs as a single process (target polling and the web
                # dashboard share one asyncio event loop — see
                # vigil/__main__.py). This dev script just runs it.
                vigil-run = pkgs.writeShellScriptBin "vigil-run" ''
                    set -e
                    # Find project root (where pyproject.toml is)
                    VIGIL_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
                    while [ "$VIGIL_ROOT" != "/" ] && [ ! -f "$VIGIL_ROOT/pyproject.toml" ]; do
                        VIGIL_ROOT=$(dirname "$VIGIL_ROOT")
                    done

                    export PYTHONPATH="$VIGIL_ROOT:$PYTHONPATH"

                    echo "Starting Vigil on http://localhost:8080"
                    exec python3 -m vigil --config "$VIGIL_ROOT/config.yaml" --port 8080 "$@"
                '';
            in
            {
                packages.default = vigil-pkg;
                packages.agent = vigil-agent-pkg;

                apps.default = {
                    type = "app";
                    program = "${vigil-pkg}/bin/vigil";
                };

                devShells.default = pkgs.mkShell {
                    buildInputs = [
                        (pkgs.python312.withPackages (
                            ps:
                            pythonDeps
                            ++ [
                                ps.pip
                                ps.setuptools
                                ps.pytest
                                ps.pytest-asyncio
                            ]
                        ))
                        vigil-run
                    ];

                    shellHook = ''
                        # Identify project root and set PYTHONPATH
                        VIGIL_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
                        export PYTHONPATH="$VIGIL_ROOT:$PYTHONPATH"

                        echo "Vigil development environment loaded."
                        echo "Python: $(python3 --version)"
                        echo ""
                        echo "Commands:"
                        echo "  vigil-run              Start the application"
                        echo "  pytest                 Run all tests"
                        echo "  pytest tests/plugins/  Run plugin tests only"
                        echo "  pytest tests/unit/     Run unit tests only"
                        echo "  pytest -v              Verbose test output"
                        echo "  pytest -k <name>       Run tests matching name"
                    '';
                };
            }
        ) // {
            nixosModules.vigil = import ./nix/module.nix self;
            nixosModules.default = import ./nix/module.nix self;
            # The monitored-host half. Enabled on every host Vigil watches
            # through an agent; the server module above is enabled only on the
            # machine running the dashboard.
            nixosModules.agent = import ./nix/agent-module.nix self;
        };
}
