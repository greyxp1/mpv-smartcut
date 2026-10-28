{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = {nixpkgs, ...}: {
    packages = nixpkgs.lib.genAttrs ["x86_64-linux" "aarch64-linux"] (system: {
      default = nixpkgs.legacyPackages.${system}.callPackage ./package.nix {};
    });
  };
}
