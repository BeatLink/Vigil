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
                    export PYTHONPATH="$PYTHONPATH:$(pwd)"
                    echo "Starting Vigil on http://localhost:8080"
                    exec python3 vigil/core/main.py --config config.yaml --port 8080 "$@"
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
                            ]
                        ))
                        vigil-run
                    ];

                    shellHook = ''
                        export PYTHONPATH="$PYTHONPATH:$(pwd)"
                        echo "Vigil development environment loaded."
                        echo "Python: $(python3 --version)"
                        echo ""
                        echo "Type 'vigil-run' to start the application."
                    '';
                };
            }
        );
}
