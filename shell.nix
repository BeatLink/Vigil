{
    pkgs ? import <nixpkgs> { },
}:

let
    pythonEnv = pkgs.python312.withPackages (
        ps: with ps; [
            paramiko
            requests
            pyyaml
            peewee
            nicegui
            setuptools
        ]
    );
in
pkgs.mkShell {
    buildInputs = [ pythonEnv ];
}
