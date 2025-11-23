{ config, lib, pkgs, ... }:

with lib;

let
  # Das Python-Skript wird hier verpackt.
  # Wir nutzen pkgs.writers.writePython3Bin wie im Beispiel.
  usb-monitor-exporter = pkgs.writers.writePython3Bin "usb-monitor-exporter" {
    libraries = with pkgs.python3Packages; [
      prometheus_client
    ];
    # Ignoriere unwichtige Linter-Warnungen beim Bauen
    #flakeIgnore = [ "E501" "F811" "F841" "W293" "E302" "F821" "E265" ];
  } (builtins.readFile ./usb-monitor-exporter.py);

in
{
  options = {
    services.usb-monitor-exporter = {
      enable = mkOption {
        type = types.bool;
        default = false;
        description = "Aktiviert den USB Monitor Prometheus Exporter.";
      };

      openFirewall = mkOption {
        type = types.bool;
        default = false;
        description = "Öffnet Port 8000 in der Firewall automatisch.";
      };
    };
  };

  config = mkIf config.services.usb-monitor-exporter.enable {
    # Stelle sicher, dass das Paket im System verfügbar ist (optional, aber nützlich für Debugging)
    environment.systemPackages = [ usb-monitor-exporter ];

    # WICHTIG: Das Skript benötigt das usbmon Kernel-Modul
    boot.kernelModules = [ "usbmon" ];

    # Firewall Port öffnen, falls gewünscht
    networking.firewall.allowedTCPPorts = mkIf config.services.usb-monitor-exporter.openFirewall [ 8000 ];

    # Systemd-Dienst Definition
    systemd.services.usb-monitor-exporter = {
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      description = "Prometheus USB Monitor Exporter";
      
      # Das Skript greift auf /sys/kernel/debug/usb/usbmon zu.
      # Dies erfordert in der Regel Root-Rechte.
      serviceConfig = {
        ExecStart = "${usb-monitor-exporter}/bin/usb-monitor-exporter";
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