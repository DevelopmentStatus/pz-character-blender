<#
Native, modern folder picker -- the same Explorer-style dialog most current
apps use, with real "Select Folder" / "Cancel" buttons (search box, sidebar,
recent locations). This is Windows' IFileOpenDialog with FOS_PICKFOLDERS.

The VBScript approach (a pick_folder.vbs, not shipped here) can only show the
OLDER "Browse For Folder" tree-view dialog, because VBScript can only call
IDispatch-based COM objects, and IFileOpenDialog isn't one -- it needs the
vtable-level interop below, which only a real .NET/COM host (PowerShell
here) can do.

Prints the chosen path to stdout, or nothing at all if cancelled -- same
contract as the VBScript version, so a caller's `for /f` capture doesn't
care which one is behind it.

Run via: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\pick_folder.ps1
#>

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

namespace PzFolderPicker {
    [ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")]
    internal class FileOpenDialogRCW { }

    [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IShellItem {
        void BindToHandler();
        void GetParent();
        void GetDisplayName(int sigdnName, out IntPtr ppszName);
        void GetAttributes();
        void Compare();
    }

    // Vtable order matters more than names here -- these mirror IModalWindow
    // + IFileDialog + IFileOpenDialog from the Windows SDK's ShObjIdl_core.h
    // exactly, in declaration order. Methods this script never calls are
    // still declared (as bare `void`, ignoring their real parameters) purely
    // to hold their vtable slot -- skip one and every call after it lands on
    // the wrong native method.
    [ComImport, Guid("d57c7288-d4ad-4768-be02-9d969532d960"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IFileOpenDialog {
        [PreserveSig] int Show(IntPtr parent);          // IModalWindow
        void SetFileTypes();                             // IFileDialog...
        void SetFileTypeIndex();
        void GetFileTypeIndex();
        void Advise();
        void Unadvise();
        void SetOptions(uint fos);
        void GetOptions();
        void SetDefaultFolder();
        void SetFolder();
        void GetFolder();
        void GetCurrentSelection();
        void SetFileName();
        void GetFileName();
        void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
        void SetOkButtonLabel();
        void SetFileNameLabel();
        void GetResult(out IShellItem ppsi);
        void AddPlace();
        void SetDefaultExtension();
        void Close();
        void SetClientGuid();
        void ClearClientData();
        void SetFilter();
        void GetResults();                                // IFileOpenDialog
        void GetSelectedItems();
    }

    public static class FolderPicker {
        private const uint FOS_PICKFOLDERS = 0x20;
        private const uint FOS_FORCEFILESYSTEM = 0x40;
        private const int SIGDN_FILESYSPATH = unchecked((int)0x80058000);

        // Returns the picked path, or null if the dialog was cancelled.
        public static string Pick(string title) {
            var dialog = (IFileOpenDialog)new FileOpenDialogRCW();
            dialog.SetOptions(FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM);
            dialog.SetTitle(title);
            int hr = dialog.Show(IntPtr.Zero);
            if (hr != 0) return null; // e.g. HRESULT_FROM_WIN32(ERROR_CANCELLED)

            IShellItem item;
            dialog.GetResult(out item);
            IntPtr pszPath;
            item.GetDisplayName(SIGDN_FILESYSPATH, out pszPath);
            try {
                return Marshal.PtrToStringUni(pszPath);
            } finally {
                Marshal.FreeCoTaskMem(pszPath);
            }
        }
    }
}
"@

$path = [PzFolderPicker.FolderPicker]::Pick("Select your Project Zomboid install folder (the one containing 'media')")
if ($path) { Write-Output $path }
