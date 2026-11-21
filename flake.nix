{
  description = "aurora_local: local Home Assistant integration and library for Adlar Castra heat pumps";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      # nix develop: Python (the version Home Assistant uses) and uv from nixpkgs; Python packages come from
      # uv.lock (uv sync), so both layers are pinned.
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.python314 pkgs.uv ];
          env = {
            UV_PYTHON = "${pkgs.python314}/bin/python3";
            UV_PYTHON_DOWNLOADS = "never";
          };
        };
      });
    };
}
