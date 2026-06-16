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

    vigil-run = pkgs.writeShellScriptBin "vigil-run" ''
        export PYTHONPATH="$PYTHONPATH:$(pwd)"
        echo "Starting Vigil on http://localhost:8080"
        exec python3 vigil/core/main.py --config config.yaml --port 8080 "$@"
    '';
in
pkgs.mkShell {
    buildInputs = [
        pythonEnv
        vigil-run
    ];
    shellHook = ''
        export PYTHONPATH="$PYTHONPATH:$(pwd)"
        echo "Vigil development environment loaded. Type 'vigil-run' to start the application."
    '';
}
