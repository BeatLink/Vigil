# Vigil agent
#
# The companion daemon for a monitored host. It dials outward to the Vigil
# server and holds one WebSocket open, carrying both the commands the server
# asks it to run and the events it observes locally.
#
# Nothing listens here: the host opens no port and needs no certificate of its
# own, so this is safe to enable on a machine behind NAT or with a closed
# firewall.
#
# The agent runs unprivileged. Plugins that need root (smartctl, systemctl
# actions) reach it the same way they did over SSH — through scoped NOPASSWD
# sudo rules the deployment grants to `user`. Do not run this as root to avoid
# writing those rules; the rules are the narrower grant.
self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.vigil-agent;
  inherit (lib)
    mkEnableOption
    mkOption
    mkIf
    types
    literalExpression
    ;

  settings =
    {
      url = cfg.url;
      id = cfg.id;
    }
    // lib.optionalAttrs (cfg.tokenFile != null) { token_file = toString cfg.tokenFile; }
    // lib.optionalAttrs (cfg.hostname != null) { hostname = cfg.hostname; }
    // cfg.extraSettings;

  # Only the token's *path* reaches this file, so the generated YAML is safe to
  # live in the world-readable Nix store; the agent reads the secret at runtime.
  configFile = (pkgs.formats.yaml { }).generate "vigil-agent.yaml" settings;

in
{
  options.services.vigil-agent = {
    enable = mkEnableOption "Vigil agent";

    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.agent;
      defaultText = literalExpression "vigil-agent (from flake)";
      description = ''
        The package providing the `vigil-agent` binary. Defaults to the
        standalone agent package, which carries only what the agent imports —
        deliberately not the server package, so a monitored host never builds
        nicegui, peewee, dnspython or asyncssh to run an agent.
      '';
    };

    url = mkOption {
      type = types.str;
      example = "ws://vigil.example.com:8080/api/agent/ws";
      description = ''
        The Vigil server's agent endpoint. Use `wss://` when the dashboard is
        behind TLS. This is an outbound connection, so only egress to the
        server's dashboard port is required.
      '';
    };

    id = mkOption {
      type = types.str;
      example = "web-01";
      description = ''
        This agent's identity. Must match an `id` in the server's `agents:`
        list, and is what a monitor's `agent:` key refers to.
      '';
    };

    tokenFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "/run/secrets/vigil_agent_token";
      description = ''
        Path to a file on this host containing the shared token this agent
        authenticates with, matching the `token` declared for its `id` on the
        server. The agent reads it at runtime, so the token never enters the
        Nix store — point this at a sops-nix/agenix-managed secret readable by
        <option>user</option>.
      '';
    };

    hostname = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = ''
        Hostname reported to the server, shown in its logs. Defaults to the
        machine's own hostname.
      '';
    };

    user = mkOption {
      type = types.str;
      default = "vigil-agent";
      description = ''
        User the agent runs as, and therefore the user every command the server
        sends executes as. Grant it the group memberships and scoped sudo rules
        the monitors on this host need.
      '';
    };

    group = mkOption {
      type = types.str;
      default = "vigil-agent";
      description = "Group the agent runs as.";
    };

    extraGroups = mkOption {
      type = types.listOf types.str;
      default = [ ];
      example = [ "systemd-journal" ];
      description = ''
        Supplementary groups for <option>user</option>. `systemd-journal` is
        needed for the journal watcher and for any monitor that reads a unit's
        logs.
      '';
    };

    path = mkOption {
      type = types.listOf (types.either types.package types.str);
      default = [ ];
      example = literalExpression ''[ pkgs.smartmontools "/run/current-system/sw" ]'';
      description = ''
        Extra entries on the agent's PATH. The commands monitors send are plain
        shell, so anything they invoke must be resolvable here.

        Strings are allowed so a deployment can pass
        `"/run/current-system/sw"`, giving the agent the same PATH an SSH login
        to this host would have seen — the closest match to the agentless
        behaviour when migrating existing monitors.
      '';
    };

    extraSettings = mkOption {
      type = (pkgs.formats.yaml { }).type;
      default = { };
      description = "Additional keys merged into the generated agent config.";
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.tokenFile != null || cfg.extraSettings ? token;
        message = ''
          services.vigil-agent: set tokenFile (preferred) or extraSettings.token.
          Without a token the agent cannot authenticate and the server will
          refuse every connection.
        '';
      }
    ];

    systemd.services.vigil-agent = {
      description = "Vigil agent (monitored-host companion)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      # The agent runs whatever shell the server sends, so the tools those
      # commands invoke must be on PATH. systemd and coreutils cover the common
      # monitors; anything else comes from `path`.
      path = [
        pkgs.coreutils
        pkgs.systemd
        pkgs.procps
        pkgs.util-linux
        pkgs.gnugrep
        pkgs.gnused
        pkgs.gawk
        pkgs.bash
      ]
      ++ cfg.path;

      serviceConfig = {
        ExecStart = "${cfg.package}/bin/vigil-agent --config ${configFile}";
        User = cfg.user;
        Group = cfg.group;

        # The server restarts, the network flaps, the agent redials on its own
        # with backoff; Restart here only covers the agent itself dying.
        Restart = "always";
        RestartSec = "10s";

        # Deliberately mild hardening. The agent's whole job is to run the
        # commands the server sends, so sandboxing it away from the system it
        # monitors would defeat the point — ProtectSystem="strict" would hide
        # the very paths disk and filesystem monitors read. Confinement comes
        # from running unprivileged with narrow sudo rules, not from here.
        NoNewPrivileges = false; # sudo rules for smartctl/systemctl need this
        PrivateTmp = true;
        ProtectHome = true;
        RestrictRealtime = true;
        LockPersonality = true;
      };
    };

    users.users.${cfg.user} = lib.mkIf (cfg.user == "vigil-agent") {
      isSystemUser = true;
      group = cfg.group;
      description = "Vigil agent service user";
      extraGroups = cfg.extraGroups;
      # Commands arrive as shell strings and are run through a shell, so this
      # account needs a real one.
      shell = pkgs.bashInteractive;
    };

    users.groups.${cfg.group} = lib.mkIf (cfg.group == "vigil-agent") { };
  };
}
