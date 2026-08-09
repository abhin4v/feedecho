# NixOS

FeedEcho ships a Nix flake and a NixOS module for declarative deployments.

## Flake (recommended)

```nix
# flake.nix in your NixOS config
{
  inputs.feedecho.url = "github:jcrabapple/feedecho";
  # ...
  outputs = { self, nixpkgs, feedecho, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      modules = [
        feedecho.nixosModules.default
        {
          services.feedecho = {
            enable = true;
            authTokenFile = "/run/secrets/feedecho-token";
            callbackUrl = "https://feedecho.example.com/oauth/callback";
            openFirewall = true;
          };
        }
      ];
    };
  };
}
```

### Dev shell

```bash
nix develop github:jcrabapple/feedecho
# or from a checkout:
nix develop
```

### Build / test

```bash
nix build
nix build .#feedecho.check  # run the test suite
```

## Non-flake NixOS (legacy channel)

Add to your `configuration.nix`:

```nix
{ pkgs, ... }:
let
  feedecho = pkgs.callPackage (fetchTree {
    type = "github";
    owner = "jcrabapple";
    repo = "feedecho";
    rev = "v1.5.0";
  } + "/nix/package.nix") { };
in {
  # Import the module manually or inline the service — see nix/module.nix
  # for the systemd service definition.
}
```

## Auth token

Write your token to a file readable by the `feedecho` user:

```bash
echo -n "your-long-random-token" > /run/secrets/feedecho-token
chmod 400 /run/secrets/feedecho-token
chown feedecho:feedecho /run/secrets/feedecho-token
```

Use `sops-nix` or `agenix` to manage the secret file declaratively.

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `services.feedecho.enable` | `false` | Enable the service |
| `services.feedecho.port` | `8453` | Web UI port |
| `services.feedecho.authTokenFile` | (required) | Path to file containing auth token |
| `services.feedecho.callbackUrl` | `http://localhost:8453/oauth/callback` | Public OAuth callback URL |
| `services.feedecho.dataDir` | `/var/lib/feedecho` | SQLite database directory |
| `services.feedecho.openFirewall` | `false` | Open firewall for the port |

## Notes

- The app runs as a dedicated `feedecho` system user
- SQLite lives in `dataDir` (default `/var/lib/feedecho`)
- Put nginx or Caddy in front for TLS — point the reverse proxy at port `8453`
- The flake uses `nixos-unstable`; pin to a specific nixpkgs revision if you need reproducibility
