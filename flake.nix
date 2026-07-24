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
                    # vigil/core/connectors/ssh_connector.py) — Vigil's only
                    # way of talking to monitored hosts.
                    asyncssh
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

                # Vigil runs as a single process (target polling and the web
                # dashboard share one asyncio event loop — see
                # vigil/core/app/main.py). This dev script just runs it.
                vigil-run = pkgs.writeShellScriptBin "vigil-run" ''
                    set -e
                    # Find project root (where pyproject.toml is)
                    VIGIL_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
                    while [ "$VIGIL_ROOT" != "/" ] && [ ! -f "$VIGIL_ROOT/pyproject.toml" ]; do
                        VIGIL_ROOT=$(dirname "$VIGIL_ROOT")
                    done

                    export PYTHONPATH="$VIGIL_ROOT:$PYTHONPATH"

                    echo "Starting Vigil on http://localhost:8080"
                    exec python3 -m vigil.core.app.main --config "$VIGIL_ROOT/config.yaml" --port 8080 "$@"
                '';
            in
            {
                packages.default = vigil-pkg;

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
        };
}
