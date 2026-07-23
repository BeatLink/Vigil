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

  # Both processes read internal_api.host/port from the same config file —
  # the collector to know where to bind, the web process to know where to
  # proxy to — so they always agree without a second option to keep in sync.
  finalSettings =
    if withAuth != null then
      withAuth
      // {
        internal_api = {
          host = cfg.internalApiHost;
          port = cfg.internalApiPort;
        };
      }
    else
      withAuth;

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

    internalApiPort = mkOption {
      type = types.port;
      default = 8081;
      description = ''
        Port for the collector process's internal API, which the web process
        uses to proxy actions (restart a service, poll now, job control,
        push-monitor heartbeats) to live plugin instances that only exist in
        the collector. Bound to loopback only — see
        <option>services.vigil.internalApiHost</option> — and must never be
        exposed to the network: reaching it lets a caller run pre-built
        commands on any monitored host via a plugin's SSHController.
      '';
    };

    internalApiHost = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = ''
        Bind address for the collector's internal API. Loopback by default;
        only change this if the collector and web processes run on different
        hosts, and firewall the port yourself if you do — Vigil applies no
        authentication of its own to this endpoint.
      '';
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

    # Vigil is two processes sharing one SQLite database (see
    # vigil/core/main.py and vigil/core/web_engine.py for the split
    # rationale): a collector that polls targets and owns the internal API,
    # and a web process that serves the dashboard and proxies actions to the
    # collector over loopback HTTP. Split into two systemd services so the
    # web process's sandboxing doesn't need the SSH/ICMP capabilities the
    # collector requires, and so either can restart independently.
    systemd.services.vigil-collector = {
      description = "Vigil Collector (target polling)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];

      # ping (uptime plugin, run as a local subprocess) must be on PATH.
      # The system `ssh` client is NOT needed here — Vigil speaks SSH
      # natively via asyncssh (see core/common/ssh_connector.py) rather than
      # shelling out.
      path = [
        pkgs.iputils
      ];

      # The known_hosts file Vigil's own TOFU host-key trust persists to
      # (see ssh_connector.py's _TofuClient) lives here — writable, and
      # ProtectHome hides ~/.ssh. Under the service's private /tmp.
      environment.VIGIL_SSH_CONTROL_DIR = "/tmp/vigil-ssh";

      serviceConfig = {
        ExecStart = "${cfg.package}/bin/vigil-collector --config ${configFile} --db ${cfg.dataDir}/vigil.db";
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

    systemd.services.vigil-web = {
      description = "Vigil Web Dashboard";
      wantedBy = [ "multi-user.target" ];
      # Not a hard dependency (After, not BindsTo/Requires): the dashboard
      # can start and serve historical data — everything already in the
      # database — even if the collector is mid-restart. Live actions just
      # fail with "collector unreachable" until it's back (see
      # CollectorClient's error handling), rather than the dashboard itself
      # refusing to come up.
      after = [
        "network.target"
        "vigil-collector.service"
      ];

      serviceConfig = {
        ExecStart = "${cfg.package}/bin/vigil-web --config ${configFile} --db ${cfg.dataDir}/vigil.db --port ${toString cfg.port}";
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
        # Needs the dataDir too: it opens the same SQLite file for reads, and
        # its own writer thread persists UI preferences (drawer width,
        # sidebar tree expanded state) into the same database.
        ReadWritePaths = [ cfg.dataDir ];
        # No CAP_NET_RAW/openssh here — this process never touches a
        # monitored host directly, only the collector's internal API over
        # loopback TCP.
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
