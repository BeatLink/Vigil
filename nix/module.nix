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

  # Recursively inject the default borg passphrase_file into every `borg`
  # monitor that doesn't already set one, walking the nested plugin tree
  # (borg monitors usually live under group `children`). The file is read by
  # Vigil at runtime on this host, so the secret never enters the Nix store.
  injectBorgPassphrase =
    plugins:
    map (
      p:
      let
        withChildren =
          if p ? children then p // { children = injectBorgPassphrase p.children; } else p;
      in
      if (p.type or null) == "borg" && !(p ? passphrase_file) && !(p ? passphrase) then
        withChildren // { passphrase_file = cfg.borgPassphraseFile; }
      else
        withChildren
    ) plugins;

  withBorgPassphrase =
    if cfg.settings != null && cfg.borgPassphraseFile != null && cfg.settings ? plugins then
      cfg.settings // { plugins = injectBorgPassphrase cfg.settings.plugins; }
    else
      cfg.settings;

  # Merge in dashboard/API auth when both authUsername and authPasswordFile
  # are set. Only the password *path* is written to the generated YAML (which
  # lands in the Nix store) — Vigil reads the file itself at runtime, so the
  # password never enters the store.
  withAuth =
    if withBorgPassphrase != null && cfg.authUsername != null && cfg.authPasswordFile != null then
      withBorgPassphrase
      // {
        auth = {
          username = cfg.authUsername;
          password_file = cfg.authPasswordFile;
        };
      }
    else
      withBorgPassphrase;

  finalSettings = withAuth;

  configFile =
    if cfg.configFile != null then
      cfg.configFile
    else if finalSettings != null then
      (pkgs.formats.yaml { }).generate "vigil.yaml" finalSettings
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

    borgPassphraseFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "/run/secrets/borg_laptop_passphrase";
      description = ''
        Path to a file on this host containing the passphrase for encrypted
        borg repositories. Injected as `passphrase_file` into every `borg`
        monitor in <option>settings</option> that does not set its own
        `passphrase_file` or `passphrase`.

        Vigil reads the file at runtime and passes the passphrase to the remote
        borg command, so the secret lives only on the machine Vigil runs on —
        the monitored hosts need no copy. Point this at a
        sops-nix/agenix-managed secret readable by the Vigil service user; the
        value never enters the Nix store.
      '';
    };

    authUsername = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "admin";
      description = ''
        Username required for HTTP Basic Auth on the dashboard and REST API.
        Must be set together with <option>authPasswordFile</option> to enable
        auth; if only one is set, Vigil logs a warning and leaves the
        dashboard/API unauthenticated.
      '';
    };

    authPasswordFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "/run/secrets/vigil_dashboard_password";
      description = ''
        Path to a file on this host containing the HTTP Basic Auth password.
        Vigil reads it at runtime, so the password never enters the Nix
        store — point this at a sops-nix/agenix-managed secret readable by
        the Vigil service user (<option>user</option>).
      '';
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = !(cfg.configFile != null && cfg.settings != null);
        message = "services.vigil: configFile and settings are mutually exclusive — set only one.";
      }
    ];

    # Vigil runs as a single process (see vigil/core/app/main.py): one
    # asyncio event loop owns both the target-polling schedule and the
    # NiceGUI web dashboard, sharing the SQLite database directly in-process
    # rather than over a loopback API.
    systemd.services.vigil = {
      description = "Vigil (network/system monitor + dashboard)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];

      # ping (uptime plugin, run as a local subprocess) must be on PATH.
      # The system `ssh` client is NOT needed here — Vigil speaks SSH
      # natively via asyncssh (see core/connectors/ssh_connector.py) rather
      # than shelling out.
      path = [
        pkgs.iputils
      ];

      # The known_hosts file Vigil's own TOFU host-key trust persists to
      # (see ssh_connector.py's _TofuClient) lives here — writable, and
      # ProtectHome hides ~/.ssh. Under the service's private /tmp.
      environment.VIGIL_SSH_CONTROL_DIR = "/tmp/vigil-ssh";

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
        # ping needs CAP_NET_RAW to open its ICMP socket; grant just that one
        # (ambient so the unprivileged service process actually receives it).
        CapabilityBoundingSet = [ "CAP_NET_RAW" ];
        AmbientCapabilities = [ "CAP_NET_RAW" ];
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
