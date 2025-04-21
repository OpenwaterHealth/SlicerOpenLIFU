# SlicerOpenLIFU

3D Slicer Extension for Openwater's OpenLIFU project

Build this extension by following [the usual procedure for Slicer extensions](https://slicer.readthedocs.io/en/latest/developer_guide/extensions.html#build-an-extension).

![Screenshot](https://github.com/OpenwaterHealth/SlicerOpenLIFU/blob/266-Publish-to-Extension-Index/blob/main/Screenshots/1.png)

## Pairing with 3D Open Water App

### Install Android Platform Tools

**macOS:**  

```bash
brew install android-platform-tools
```

**Linux:**  

```bash
sudo apt update
sudo apt install android-tools-adb
```

**Windows (PowerShell):**  

```powershell
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
irm get.scoop.sh | iex
scoop install android-platform-tools
```

Or download from [Google's
platform-tools](https://developer.android.com/tools/releases/platform-tools) and
add it to your `PATH`.

### Enable USB Debugging on Android

1. On your Android device, go to **Settings → About phone → Software information**.
2. Tap **Build number** 7 times until you see "You are now a developer!".
3. Go to **Settings → System → Developer options**.
4. Enable **USB debugging**.
5. When prompted, allow USB debugging access to your computer.  (Check "Always
   allow" to avoid repeated prompts.)

## Meshroom environment

The photoscan generation requires that the meshroom executable be in the system path in the environment in which Slicer is launched.
Follow the instructions [here](https://github.com/OpenwaterHealth/OpenLIFU-python?tab=readme-ov-file#installing-meshroom) to add download and add meshroom to the system path.
