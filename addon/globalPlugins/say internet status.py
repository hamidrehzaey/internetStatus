# -*- coding: utf-8 -*-
# internet announcement Global Plugin for NVDA
# Copyright (C) 2023 hamid rezayi <hamidrehzaey>
# This file is covered by the GNU General Public License.
# You can read the licence by clicking Help->Licence in the NVDA menu
# or by visiting http://www.gnu.org/licenses/old-licenses/gpl-2.0.html
# Shortcut: NVDA+Alt+i
#it  has been directly copied from the dropbox addon
# copyright (C) hamid rezayi

import ctypes
from ctypes import wintypes
import os
import re
import subprocess
import threading
import wx
import core
import globalPluginHandler
import addonHandler
import scriptHandler
import ui
import NVDAObjects
import api
import winUser
import controlTypes
import comtypes
import comtypes.automation
import comtypes.client
import comtypes.client.dynamic
from logHandler import log
from typing import Callable


# We initialize translation support
addonHandler.initTranslation()
_: Callable[[str], str]
# Constants for roles to keep compatibility
if hasattr(controlTypes, 'ROLE_POPUPMENU'):
	POPUP_MENU = controlTypes.ROLE_POPUPMENU
else:
	POPUP_MENU = controlTypes.Role.POPUPMENU
if hasattr(controlTypes, 'ROLE_MENUITEM'):
	MENU_ITEM = controlTypes.ROLE_MENUITEM
else:
	MENU_ITEM = controlTypes.Role.MENUITEM

CLSID_NetworkListManager = "{DCB00C01-570F-4A9B-8D69-199FDBA5723B}"
NLM_ENUM_NETWORK_CONNECTED = 1


def _getUser32():
	"""Return a private WinDLL('user32') instance with 64-bit-safe argtypes."""
	user32 = ctypes.WinDLL("user32")
	user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
	user32.FindWindowW.restype = wintypes.HWND
	user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
	user32.FindWindowExW.restype = wintypes.HWND
	user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
	user32.GetClassNameW.restype = ctypes.c_int
	return user32


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	# We initialize the scripts category shown on input gestures dialog
	scriptCategory = _("Internet Status")

	def __init__(self):
		super().__init__()
		self._menuTimer = None

	def terminate(self):
		self._cancelMenuTimer()
		try:
			super().terminate()
		except AttributeError:
			pass

	def _cancelMenuTimer(self):
		if self._menuTimer is not None:
			try:
				self._menuTimer.Stop()
			except Exception:
				pass
			self._menuTimer = None

	def _getNetworkManagerState(self):
		"""Return (isInternet, isConnected, [(name, hasInternet), ...]) from Network List Manager."""
		try:
			comtypes.CoInitialize()
		except OSError:
			pass
		nlm = comtypes.client.CreateObject(CLSID_NetworkListManager, dynamic=True)
		isInternet = bool(nlm.IsConnectedToInternet)
		isConnected = bool(nlm.IsConnected)

		networks = []
		seen = set()
		try:
			for item in nlm.GetNetworks(NLM_ENUM_NETWORK_CONNECTED):
				net = comtypes.client.dynamic.Dispatch(
					item.QueryInterface(comtypes.automation.IDispatch)
				)
				netName = net.GetName()
				hasInternet = bool(net.IsConnectedToInternet)
				if netName and (netName, hasInternet) not in seen:
					seen.add((netName, hasInternet))
					networks.append((netName, hasInternet))
		except Exception:
			log.debugWarning("internetStatus: failed enumerating networks", exc_info=True)
		return isInternet, isConnected, networks

	def _getNetworkStatusFromAPI(self):
		"""Read connectivity and active network names directly from Windows Network List Manager."""
		try:
			isInternet, isConnected, networks = self._getNetworkManagerState()
			if networks:
				networks.sort(key=lambda entry: not entry[1])
				parts = []
				for netName, hasInternet in networks:
					if hasInternet:
						parts.append(_("{network}: Internet access").format(network=netName))
					else:
						parts.append(_("{network}: No internet access").format(network=netName))
				return ". ".join(parts)

			if isInternet:
				return _("Internet access")
			if isConnected:
				return _("No internet access")
			return _("Not connected - No connections are available")
		except Exception:
			log.debugWarning("internetStatus: NetworkListManager failed", exc_info=True)
			return None

	def _runNetsh(self, args):
		"""Run netsh silently without flashing a console window and decode output."""
		si = subprocess.STARTUPINFO()
		si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
		creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
		res = subprocess.run(
			["netsh"] + args,
			capture_output=True,
			startupinfo=si,
			creationflags=creationflags,
			timeout=5,
		)
		raw = res.stdout or b""
		try:
			return res.returncode, raw.decode("utf-8")
		except UnicodeDecodeError:
			oemCp = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
			return res.returncode, raw.decode(oemCp, errors="replace")

	def _findConnectedWifiProfile(self, connectedNames):
		"""Determine the active Wi-Fi profile name on Windows 10 or Windows 11."""
		code, out = self._runNetsh(["wlan", "show", "interfaces"])
		if code == 0 and out:
			stateMatch = re.search(r"^\s*State\s*:\s*(.+)$", out, re.MULTILINE)
			if stateMatch and "connected" in stateMatch.group(1).lower():
				profileMatch = re.search(r"^\s*Profile\s*:\s*(.+)$", out, re.MULTILINE)
				if profileMatch:
					return profileMatch.group(1).strip()
				ssidMatch = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.MULTILINE)
				if ssidMatch:
					return ssidMatch.group(1).strip()

		if not connectedNames:
			return None
		code, out = self._runNetsh(["wlan", "show", "profiles"])
		if code != 0 or not out:
			return None
		savedProfiles = []
		for match in re.finditer(r"^\s*(?:All User Profile|User Profile)\s*:\s*(.+)$", out, re.MULTILINE):
			name = match.group(1).strip()
			if name and name != "<None>":
				savedProfiles.append(name)
		savedSet = set(savedProfiles)
		for name in connectedNames:
			if name in savedSet:
				return name
		lowerMap = {p.lower(): p for p in savedProfiles}
		for name in connectedNames:
			if name.lower() in lowerMap:
				return lowerMap[name.lower()]
		return None

	def _getWifiProfilePassword(self, profileName):
		"""Read plaintext Wi-Fi key content for the given saved profile."""
		code, out = self._runNetsh(["wlan", "show", "profile", f"name={profileName}", "key=clear"])
		if code != 0 or not out:
			return None
		keyMatch = re.search(r"^\s*Key Content\s*:\s*(.+)$", out, re.MULTILINE)
		if keyMatch:
			password = keyMatch.group(1).rstrip("\r\n")
			return password if password else None
		return None

	def _speakAndCopyPassword(self, profileName, password):
		if api.copyToClip(password):
			ui.message(_("{network} password copied: {password}").format(
				network=profileName,
				password=password,
			))
		else:
			ui.message(_("{network} password: {password}").format(
				network=profileName,
				password=password,
			))

	def _announceAndCopyWifiPassword(self):
		connectedNames = []
		try:
			_isInternet, _isConnected, networks = self._getNetworkManagerState()
			connectedNames = [name for name, _hasInternet in networks]
		except Exception:
			pass

		def _worker():
			try:
				profileName = self._findConnectedWifiProfile(connectedNames)
				if not profileName:
					wx.CallAfter(ui.message, _("No connected Wi-Fi network found"))
					return
				password = self._getWifiProfilePassword(profileName)
				if not password:
					wx.CallAfter(
						ui.message,
						_("No saved password found for {network}").format(network=profileName),
					)
					return
				wx.CallAfter(self._speakAndCopyPassword, profileName, password)
			except Exception:
				log.debugWarning("internetStatus: failed getting Wi-Fi password", exc_info=True)
				wx.CallAfter(ui.message, _("Unable to get Wi-Fi password"))

		threading.Thread(target=_worker, daemon=True).start()

	def _openNetworkMenu(self):
		self._menuTimer = None
		objFocused = api.getFocusObject()
		currentProcess = objFocused.appModule.appName.lower() if objFocused.appModule else ""
		if (
			currentProcess == u'internet'
			and objFocused.windowClassName.lower() == u'#32768'
			and objFocused.role in ({POPUP_MENU, MENU_ITEM})
		):
			return
		trayIcon = self._findInternetAccess()
		if trayIcon:
			try:
				trayIcon.doAction()
				return
			except Exception:
				pass
		for uri in ("ms-availablenetworks:", "ms-settings:network-status"):
			try:
				os.startfile(uri)
				return
			except Exception:
				continue
		# Translators: the message reported when it is not possible to activate internet context menu.
		ui.message(_("Unable to activate internet context menu"))

	def _getToolbarTrayObjects(self, windowClassPath):
		"""Collect tray objects along a Win32 window class path (Windows 7/8/10)."""
		user32 = _getUser32()
		h = None
		for element in windowClassPath:
			h = user32.FindWindowExW(h, None, element, None)
			if not h:
				return []
		trayObjects = []
		try:
			trayObject = NVDAObjects.IAccessible.getNVDAObjectFromEvent(h, -4, 1)
		except Exception:
			return []
		while trayObject is not None:
			if trayObject.name:
				trayObjects.append(trayObject)
			trayObject = trayObject.next
		return trayObjects

	def _findAccessibleLeafsFromWindowClassPath(self, windowClassPath):
		# Windows 10: check both primary systray toolbar and overflow tray window
		trayObjects = self._getToolbarTrayObjects(windowClassPath)
		overflowPath = ("NotifyIconOverflowWindow", "ToolbarWindow32")
		trayObjects.extend(self._getToolbarTrayObjects(overflowPath))
		return trayObjects

	def _findAccessibleLeafsFromWindowClassPath11(self, windowClassPath):
		# Windows 11 (21H2) systray icons
		user32 = _getUser32()
		h = None
		for className in windowClassPath:
			h = user32.FindWindowExW(h, None, className, None)
			if not h:
				return []
		try:
			obj = NVDAObjects.IAccessible.getNVDAObjectFromEvent(h, -4, 0).firstChild.children
		except Exception:
			return []
		return list(obj) if obj else []

	def _findAccessibleLeafsFromWindowClassPath11_22h2(self):
		"""
		Create a list of systray icons across all InputSite windows in Shell_TrayWnd.
		"""
		user32 = _getUser32()
		h = user32.FindWindowW("Shell_TrayWnd", None)
		if not h:
			return []
		inputSiteHwnds = []
		EnumChildProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

		def _enumChild(hwnd, lParam):
			buf = ctypes.create_unicode_buffer(256)
			user32.GetClassNameW(hwnd, buf, 256)
			if buf.value == "Windows.UI.Input.InputSite.WindowClass":
				inputSiteHwnds.append(hwnd)
			return True

		user32.EnumChildWindows(h, EnumChildProc(_enumChild), 0)
		trayObjects = []
		for hwnd in inputSiteHwnds:
			try:
				rootObj = NVDAObjects.IAccessible.getNVDAObjectFromEvent(hwnd, -4, 0)
				if rootObj and rootObj.children:
					trayObjects.extend(rootObj.children)
			except Exception:
				continue
		return trayObjects

	def _findInternetAccess(self):
		path = ("Shell_TrayWnd", "TrayNotifyWnd", "SysPager", "ToolbarWindow32")
		path11 = ("Shell_TrayWnd", "TrayNotifyWnd", "Windows.UI.Composition.DesktopWindowContentBridge")
		win11_22h2 = False
		win11 = False
		try:
			from winVersion import getWinVer, WinVersion
			win11_22h2 = getWinVer() >= WinVersion(major=10, minor=0, build=22621)
			win11 = getWinVer() >= WinVersion(major=10, minor=0, build=22000)
		except ImportError:
			pass
		if win11_22h2:
			objects = self._findAccessibleLeafsFromWindowClassPath11_22h2()
		elif win11:
			objects = self._findAccessibleLeafsFromWindowClassPath11(path11)
		else:
			objects = self._findAccessibleLeafsFromWindowClassPath(path)
		for trayIcon in objects:
			iconName = trayIcon.name
			if not iconName:
				continue
			lower = iconName.lower()
			if (
				"internet access" in lower
				or "no internet" in lower
				or "not connected - no connections are available" in lower
				or "not connected - connections are available" in lower
				or lower.startswith("network ")
			):
				# internet object found
				return trayIcon
		return None

	def script_announceInternetAccess(self, gesture):
		repeatCount = scriptHandler.getLastScriptRepeatCount()
		self._cancelMenuTimer()
		if repeatCount == 0:
			status = self._getNetworkStatusFromAPI()
			if status:
				ui.message(status)
				return
			trayIcon = self._findInternetAccess()
			if not trayIcon:
				# Translators: the message presented when internet tray icon was not found
				ui.message(_("internet  icon not found"))
				return
			name = " ".join(trayIcon.name.split())
			ui.message(name)
		elif repeatCount == 1:
			# Delay slightly so a quick third press cancels opening the menu.
			self._menuTimer = core.callLater(550, self._openNetworkMenu)
		elif repeatCount == 2:
			self._announceAndCopyWifiPassword()
	# Translators: message presented when user performs input help for the NVDA+Alt+i script
	script_announceInternetAccess.__doc__ = _(
		"If pressed once, announces internet status. "
		"If pressed twice, opens the internet menu. "
		"If pressed three times, speaks and copies the connected Wi-Fi password"
	)

	__gestures = {
		"kb:NVDA+alt+i": "announceInternetAccess",
	}
