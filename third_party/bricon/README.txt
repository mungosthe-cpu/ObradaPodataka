BRICON USB drajveri (zvanicni sa bricon.be)
============================================

Instalacioni paket TaurisPdfApp ukljucuje ove drajvere i instalira ih
automatski (admin) pri prvoj instalaciji programa.

Izvori (preuzimaju se pri build_installer.ps1):
  - CDM_Setup.exe (Win 10/11)
    https://www.bricon.be/download/software/Drivers/CDM_Setup.exe
  - USBDriver_win7.zip -> USBDriverInstallerV2.04.16.exe (Win 7 / stariji)
    https://www.bricon.be/download/software/Drivers/USBDriver_win7.zip

Autorska prava: BRICON nv / FTDI (CDM). Koristite samo uz BRICON opremu.

Fajlovi u drivers/ nisu u git-u (velicina); build ih preuzima pre Inno Setup-a.
