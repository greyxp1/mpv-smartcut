{
  lib,
  lua5_4,
  python3Packages,
}:
python3Packages.buildPythonApplication {
  pname = "mpv-smartcut";
  version = "0.1.0";
  pyproject = true;
  src = lib.cleanSource ./.;

  build-system = with python3Packages; [setuptools wheel];
  dependencies = [python3Packages.av];

  postInstall = ''
    install -Dm444 mpv-smartcut.lua \
      "$out/share/mpv/scripts/mpv-smartcut.lua"
    install -Dm444 mpv-smartcut.conf.example \
      "$out/share/mpv/script-opts/mpv-smartcut.conf"
  '';

  nativeInstallCheckInputs = [lua5_4];
  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    luac -p mpv-smartcut.lua
    runHook postInstallCheck
  '';
  pythonImportsCheck = ["smartcut"];

  meta = {
    description = "Frame-accurate smart cutting backend and mpv frontend";
    license = lib.licenses.mit;
    mainProgram = "mpv-smartcut-backend";
    platforms = lib.platforms.unix;
  };
}
