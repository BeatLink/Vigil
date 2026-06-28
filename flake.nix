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
                pkgs = import nixpkgs { inherit system; };
                pyproject = builtins.fromTOML (builtins.readFile ./pyproject.toml);

                pythonDeps = with pkgs.python312Packages; [
                    paramiko
                    requests
                    pyyaml
                    peewee
                    nicegui
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

                vigil-run = pkgs.writeShellScriptBin "vigil-run" ''
                    # Find project root (where pyproject.toml is)
                    VIGIL_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
                    while [ "$VIGIL_ROOT" != "/" ] && [ ! -f "$VIGIL_ROOT/pyproject.toml" ]; do
                        VIGIL_ROOT=$(dirname "$VIGIL_ROOT")
                    done

                    export PYTHONPATH="$VIGIL_ROOT:$PYTHONPATH"
                    echo "Starting Vigil on http://localhost:8080"

                    # Use module execution to handle absolute imports correctly
                    exec python3 -m vigil --config "$VIGIL_ROOT/config.yaml" --port 8080 "$@"
                '';
            in
            {
                packages.default = vigil-pkg;

                apps.vigil = {
                    type = "app";
                    program = "${vigil-pkg}/bin/vigil";
                };

                apps.default = self.apps.${system}.vigil;

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
        );
}
