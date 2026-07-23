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
                    # Async HTTP client the web process uses to reach the
                    # collector's internal API (see
                    # vigil/core/modules/controllers/remote_proxy.py).
                    httpx
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

                # Vigil runs as two processes: a collector (polls targets, owns
                # the internal API) and a web process (dashboard, proxies
                # actions to the collector). This dev script starts both —
                # collector in the background, web process in the foreground
                # so Ctrl-C stops the whole thing (the collector is killed
                # along with the shell's job group on script exit).
                vigil-run = pkgs.writeShellScriptBin "vigil-run" ''
                    set -e
                    # Find project root (where pyproject.toml is)
                    VIGIL_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
                    while [ "$VIGIL_ROOT" != "/" ] && [ ! -f "$VIGIL_ROOT/pyproject.toml" ]; do
                        VIGIL_ROOT=$(dirname "$VIGIL_ROOT")
                    done

                    export PYTHONPATH="$VIGIL_ROOT:$PYTHONPATH"

                    echo "Starting Vigil collector (internal API on http://127.0.0.1:8081)"
                    python3 -m vigil.collector --config "$VIGIL_ROOT/config.yaml" &
                    collector_pid=$!
                    trap 'kill "$collector_pid" 2>/dev/null' EXIT

                    echo "Starting Vigil web dashboard on http://localhost:8080"
                    exec python3 -m vigil.web --config "$VIGIL_ROOT/config.yaml" --port 8080 "$@"
                '';
            in
            {
                packages.default = vigil-pkg;

                # Vigil is two binaries now (vigil-collector, vigil-web — see
                # pyproject.toml's [project.scripts]); `nix run` has no single
                # process to hand back, so apps.default runs both via the same
                # dev script devShells.default exposes as `vigil-run`.
                apps.vigil-collector = {
                    type = "app";
                    program = "${vigil-pkg}/bin/vigil-collector";
                };

                apps.vigil-web = {
                    type = "app";
                    program = "${vigil-pkg}/bin/vigil-web";
                };

                apps.default = {
                    type = "app";
                    program = "${vigil-run}/bin/vigil-run";
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
