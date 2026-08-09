{
  description = "FeedEcho — self-hosted RSS feed cross-poster";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      # Helper to build the package for a given system
      mkPackage = system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python312;
        in
        python.pkgs.buildPythonApplication {
          pname = "feedecho";
          version = "1.5.0";
          src = ./.;
          format = "pyproject";

          nativeBuildInputs = [ python.pkgs.hatchling ];
          build-system = [ python.pkgs.hatchling ];

          propagatedBuildInputs = with python.pkgs; [
            fastapi
            uvicorn
            jinja2
            python-multipart
            feedparser
            httpx
            apscheduler
          ];

          nativeCheckInputs = with python.pkgs; [
            pytest
            pytest-asyncio
          ];

          # Tests need a writable temp DB; safe to run in checkPhase.
          checkPhase = ''
            runHook preCheck
            python -m pytest tests/ -q
            runHook postCheck
          '';
          doCheck = false;

          meta = with pkgs.lib; {
            description = "Self-hosted RSS feed cross-poster — route feed items to Mastodon";
            homepage = "https://github.com/jcrabapple/feedecho";
            license = licenses.mit;
            mainProgram = "uvicorn";
            platforms = platforms.linux ++ platforms.darwin;
          };
        };
    in
    {
      # Expose the package per-system
      packages = builtins.listToAttrs (
        builtins.map
          (system: {
            name = system;
            value = {
              default = mkPackage system;
              feedecho = mkPackage system;
            };
          })
          nixpkgs.lib.systems.flakeExposed
      );

      # NixOS module — system-independent
      nixosModules.default = import ./nix/module.nix;
      nixosModules.feedecho = import ./nix/module.nix;

      # Also expose the module at top level for `nixosModules.feedecho`
      nixosModule = import ./nix/module.nix;
    };
}
