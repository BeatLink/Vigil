self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.vigil;
  inherit (lib)
    mkEnableOption
    mkOption
    mkIf
    types
    literalExpression
    ;

  configFile =
    if cfg.configFile != null then
      cfg.configFile
    else if cfg.settings != null then
      (pkgs.formats.yaml { }).generate "vigil.yaml" cfg.settings
    else
      throw "services.vigil: either configFile or settings must be set";

in
{
  options.services.vigil = {
    enable = mkEnableOption "Vigil system monitor";

    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.system}.default;
      defaultText = literalExpression "vigil (from flake)";
      description = "The Vigil package to use.";
    };

    configFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = literalExpression "./vigil.yaml";
      description = ''
        Path to a pre-written Vigil config.yaml.
        Mutually exclusive with <option>services.vigil.settings</option>.
      '';
    };

    settings = mkOption {
      type = types.nullOr (pkgs.formats.yaml { }).type;
      default = null;
      example = literalExpression ''
        {
          plugins = [
            {
              name = "My Server";
              type = "uptime";
              target_host = "server.example.com";
            }
          ];
        }
      '';
      description = ''
        Vigil configuration as a Nix attribute set, serialized to YAML and
        passed to the service. Mutually exclusive with
        <option>services.vigil.configFile</option>.
      '';
    };

    port = mkOption {
      type = types.port;
      default = 8080;
      description = "Port for the web dashboard.";
    };

    dataDir = mkOption {
      type = types.str;
      default = "/var/lib/vigil";
      description = "Directory for persistent data (SQLite database).";
    };

    user = mkOption {
      type = types.str;
      default = "vigil";
      description = "User account under which Vigil runs.";
    };

    group = mkOption {
      type = types.str;
      default = "vigil";
      description = "Group under which Vigil runs.";
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = "Open the dashboard port in the firewall.";
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = !(cfg.configFile != null && cfg.settings != null);
        message = "services.vigil: configFile and settings are mutually exclusive — set only one.";
      }
    ];

    systemd.services.vigil = {
      description = "Vigil System Monitor";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];

      serviceConfig = {
        ExecStart = "${cfg.package}/bin/vigil --config ${configFile} --db ${cfg.dataDir}/vigil.db --port ${toString cfg.port}";
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.dataDir;
        StateDirectory = "vigil";
        StateDirectoryMode = "0750";
        Restart = "on-failure";
        RestartSec = "5s";

        PrivateTmp = true;
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.dataDir ];
        CapabilityBoundingSet = "";
        LockPersonality = true;
        RestrictNamespaces = true;
        RestrictRealtime = true;
        SystemCallFilter = [ "@system-service" ];
      };
    };

    users.users.vigil = lib.mkIf (cfg.user == "vigil") {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.dataDir;
      description = "Vigil service user";
    };

    users.groups.vigil = lib.mkIf (cfg.group == "vigil") { };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];
  };
}
