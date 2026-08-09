# NixOS module for FeedEcho
#
# Add to your configuration:
#
#   services.feedecho = {
#     enable = true;
#     authTokenFile = "/run/secrets/feedecho-token";
#     callbackUrl = "https://feedecho.example.com/oauth/callback";
#   };
#
# Then `nixos-rebuild switch`. The app runs on port 8453 by default.
# Put a reverse proxy (nginx, caddy) in front for TLS.

{ config, lib, pkgs, ... }:

let
  cfg = config.services.feedecho;
in {
  options.services.feedecho = {
    enable = lib.mkEnableOption "FeedEcho RSS feed cross-poster";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { python = pkgs.python312; };
      defaultText = lib.literalExpression "pkgs.callPackage ./nix/package.nix { }";
      description = "FeedEcho package to use.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8453;
      description = "Port for the FeedEcho web UI.";
    };

    authTokenFile = lib.mkOption {
      type = lib.types.path;
      description = ''
        Path to a file containing the auth token for the FeedEcho web UI.
        Use <literal>services.feedecho.authToken</literal> for testing only —
        prefer a secret file for real deployments.
      '';
    };

    callbackUrl = lib.mkOption {
      type = lib.types.str;
      default = "http://localhost:8453/oauth/callback";
      description = "Public callback URL for Mastodon OAuth.";
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/feedecho";
      description = "Directory for the SQLite database.";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open the firewall for the configured port.";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.feedecho = {
      isSystemUser = true;
      group = "feedecho";
      home = cfg.dataDir;
      createHome = true;
    };
    users.groups.feedecho = { };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];

    systemd.services.feedecho = {
      description = "FeedEcho RSS feed cross-poster";
      after = [ "network.target" ];
      wantedBy = [ "multi-user.target" ];

      environment = {
        FEEDCHO_DB_PATH = "${cfg.dataDir}/feedecho.db";
        FEEDCHO_CALLBACK_URL = cfg.callbackUrl;
      };

      serviceConfig = {
        User = "feedecho";
        Group = "feedecho";
        StateDirectory = "feedecho";
        LoadCredential = "auth_token:${cfg.authTokenFile}";
        Restart = "on-failure";
        RestartSec = 5;

        # Hardening
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.dataDir ];
      };

      # Read the auth token from the credential file and exec uvicorn.
      # WorkingDirectory is set to site-packages so app.py, templates/,
      # and static/ are all resolvable.
      script = ''
        cd "${cfg.package}/lib/python3.12/site-packages"
        export FEEDCHO_AUTH_TOKEN=$(cat "$CREDENTIALS_DIRECTORY/auth_token")
        exec ${cfg.package}/bin/uvicorn app:app --host 0.0.0.0 --port ${toString cfg.port}
      '';
    };
  };
}
