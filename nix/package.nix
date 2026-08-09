# Standalone package derivation (for non-flake NixOS users)
# Used by nix/module.nix when not consuming via flake.nix.
{ lib
, python ? python3
, hatchling
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
    hash = lib.fakeHash; # replace after first build: `nix hash-from-path ./src`
  };

  format = "pyproject";

  nativeBuildInputs = [ hatchling ];
  build-system = [ hatchling ];

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

  meta = with lib; {
    description = "Self-hosted RSS feed cross-poster";
    homepage = "https://github.com/jcrabapple/feedecho";
    license = licenses.mit;
    platforms = platforms.linux ++ platforms.darwin;
  };
}
