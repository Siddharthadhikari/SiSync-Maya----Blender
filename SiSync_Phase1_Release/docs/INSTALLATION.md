# SiSync Phase 1 — Installation, Update & Uninstall Guide (`docs/INSTALLATION.md`)

## 1. Blender 5.x Installation

1. Open **Blender 5.2 LTS**.
2. Go to **Edit > Preferences > Add-ons** (or **Get Extensions**).
3. Click the top-right dropdown (`⌄`) and select **Install from Disk...**.
4. Select `blender/SiSync_Blender_Addon.zip` (or install the `bl_sisync` folder directly into `%APPDATA%\Blender Foundation\Blender\5.2\extensions\user_default\bl_sisync`).
5. Enable **SiSync: Maya ⇄ Blender Bridge (GoB Phase 1)**.
6. The **SiSync** tab appears in the 3D Viewport Sidebar (`N`-panel) and Export/Import buttons appear in the 3D Viewport Top Header.

---

## 2. Autodesk Maya 2026 Installation & Update

1. Open **Autodesk Maya 2026**.
2. Drag and drop `maya/drag_and_drop_install.py` directly from Windows File Explorer into Maya's 3D Viewport.
3. SiSync will automatically:
   - Stop any previously running SiSync background server.
   - Create or cleanly refresh the single **`SiSync`** shelf tab (without creating duplicate shelves `SiSync1`, `SiSync2`, etc.).
   - Start the Maya BridgeServer on port `19850` and commandPort on `:19852`.
   - Open the **SiSync Maya - Blender** window.

---

## 3. Clean Uninstallation

### In Maya 2026
Run the following in Maya's Python Script Editor:
```python
import sisync_maya
sisync_maya.uninstall()
```
This stops the background TCP server (`19850`), stops the Qt auto-watch timer, closes `SiSyncWindow`, and deletes the `SiSync` shelf layout cleanly.

### In Blender 5.2
Disable or uninstall **SiSync: Maya ⇄ Blender Bridge (GoB Phase 1)** in **Edit > Preferences > Add-ons**. This automatically invokes `unregister()`, stopping the port `19851` server, unregistering `bpy.app.timers`, and removing all UI panels and header buttons.
