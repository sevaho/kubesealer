# shell.nix
{pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
    packages = [
        pkgs.kubeseal
        pkgs.python311

        (pkgs.poetry.override { python3 = pkgs.python311; })
        pkgs.zstd

        (pkgs.python311.withPackages (p: with p; [
            pip
            python-lsp-server
            pynvim
            pyls-isort
            python-lsp-black
        ]))
    ];

    LD_LIBRARY_PATH = "${pkgs.lib.makeLibraryPath [
        pkgs.stdenv.cc.cc
    ]}";
}
