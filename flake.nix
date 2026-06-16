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
                python = pkgs.python312;

                vigil-pkg = python.pkgs.buildPythonApplication {
                    pname = "vigil";
                    version = "0.1.0";
                    format = "pyproject";

                    src = ./.;

                    nativeBuildInputs = with python.pkgs; [
                        setuptools
                    ];

                    propagatedBuildInputs = with python.pkgs; [
                        paramiko
                        requests
                        pyyaml
                        peewee
                        nicegui
                    ];

                    pythonImportsCheck = [ "vigil" ];
                };
            in
            {
                packages.default = vigil-pkg;

                devShells.default = pkgs.mkShell {
                    buildInputs = with pkgs; [
                        (python.withPackages (
                            ps: with ps; [
                                paramiko
                                requests
                                pyyaml
                                peewee
                                nicegui
                                pip
                                setuptools
                            ]
                        ))
                    ];

                    shellHook = ''
                        echo "Welcome to the Vigil development environment"
                        echo "Python: $(python --version)"
                    '';
                };
            }
        );
}
