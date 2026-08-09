# Standalone package derivation (for non-flake NixOS users)
# Used by nix/module.nix default when not consuming via flake.nix.
{ lib
, python ? python3
, fetchFromGitHub ? null
, src ? null
}:

python.pkgs.buildPythonApplication {
  pname = "feedecho";
  version = "1.5.0";

  src = if src != null then src else fetchFromGitHub {
    owner = "jcrabapple";
    repo = "feedecho";
    rev = "v1.5.0";
    hash = lib.fakeHash; # replace after first build: `nix-prefetch-url --unpack <url>`
  };

  format = "pyproject";

  nativeBuildInputs = [ python.pkgs.hatchling ];

  propagatedBuildInputs = with python.pkgs; [
    fastapi
    uvicorn
    jinja2
    python-multipart
    feedparser
    httpx
    apscheduler
  ];

  nativeCheckInputs = with python.pkgs; [ pytest pytest-asyncio ];

  checkPhase = ''
    runHook preCheck
    python -m pytest tests/ -q
    runHook postCheck
  '';

  doCheck = false;

  # Expose the Python interpreter so the module can derive the correct
  # site-packages path without hardcoding "python3.12".
  passthru.python = python;

  meta = with lib; {
    description = "Self-hosted RSS feed cross-poster";
    homepage = "https://github.com/jcrabapple/feedecho";
    license = licenses.mit;
    mainProgram = "uvicorn";
    platforms = platforms.linux ++ platforms.darwin;
  };
}
