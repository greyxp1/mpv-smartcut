{
  lib,
  python3Packages,
}:
python3Packages.buildPythonApplication {
  pname = "mpv-smartcut";
  version = "0.1.0";
  pyproject = true;
  src = lib.cleanSource ./.;

  build-system = [python3Packages.setuptools];
  dependencies = [python3Packages.av];

  postInstall = ''
    install -Dm444 mpv-smartcut.lua \
      "$out/share/mpv/scripts/mpv-smartcut.lua"
    substituteInPlace "$out/share/mpv/scripts/mpv-smartcut.lua" \
      --replace-fail 'backend = "mpv-smartcut-backend"' \
        "backend = \"$out/bin/mpv-smartcut-backend\""
  '';

  passthru.scriptName = "mpv-smartcut.lua";

  pythonImportsCheck = ["smartcut.__main__"];

  meta = {
    description = "Frame-accurate smart cutting backend and mpv frontend";
    license = lib.licenses.mit;
    mainProgram = "mpv-smartcut-backend";
  };
}
