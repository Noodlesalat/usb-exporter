{
  inputs = {
    nixpkgs.url = "github:Nixos/nixpkgs/nixos-25.05";
  };

  outputs = { ... }: rec {
    nixosModules = rec {
      usbExporter = import ./default.nix;
      default = usbExporter;
    };
    nixosModule = nixosModules.default;
  };
}
