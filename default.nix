{ config, lib, pkgs, ... }:

with lib;

let
  # Das Python-Skript wird hier verpackt.
  # Wir nutzen pkgs.writers.writePython3Bin wie im Beispiel.
  usb-exporter = pkgs.writers.writePython3Bin "usb-exporter" {
    libraries = with pkgs.python3Packages; [
      prometheus_client
    ];
    # Ignoriere unwichtige Linter-Warnungen beim Bauen
    #flakeIgnore = [ "E501" "F811" "F841" "W293" "E302" "F821" "E265" ];
    flakeIgnore = [ "E501" "E261" "F541" "E305" "E722" "W291" "E226" "F401" "E302" ];
  } (builtins.readFile ./usb-exporter.py);

in
{
  options = {
    services.usb-exporter = {
      enable = mkOption {
        type = types.bool;
        default = false;
        description = "Aktiviert den USB Prometheus Exporter.";
      };

      openFirewall = mkOption {
        type = types.bool;
        default = false;
        description = "Öffnet Port 8000 in der Firewall automatisch.";
      };
    };
  };

  config = mkIf config.services.usb-exporter.enable {
    # Stelle sicher, dass das Paket im System verfügbar ist (optional, aber nützlich für Debugging)
    environment.systemPackages = [ usb-exporter ];

    # WICHTIG: Das Skript benötigt das usbmon Kernel-Modul
    boot.kernelModules = [ "usbmon" ];

    # Firewall Port öffnen, falls gewünscht
    networking.firewall.allowedTCPPorts = mkIf config.services.usb-exporter.openFirewall [ 8000 ];

    # Systemd-Dienst Definition
    systemd.services.usb-exporter = {
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      description = "Prometheus USB Exporter";
      
      # Das Skript greift auf /sys/kernel/debug/usb/usbmon zu.
      # Dies erfordert in der Regel Root-Rechte.
      serviceConfig = {
        ExecStart = "${usb-exporter}/bin/usb-exporter";
        Restart = "always";
        RestartSec = "5s";
        
        # Security hardening (optional, aber empfohlen, wenn möglich)
        # Da Zugriff auf debugfs nötig ist, ist Isolation schwierig.
        # Wir lassen es vorerst als root laufen.
        Type = "simple";
      };
    };
  };
}
