#!/usr/bin/env python3
"""
Google Drive database downloader using Workload Identity Federation.

This script downloads a file from Google Drive using the Google Drive API
with authentication via Workload Identity Federation (WIF).
"""

import os
import sys
import io
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.auth import default


def download_file_from_drive(file_id: str, output_filename: str) -> bool:
    """
    Download a file from Google Drive using the Drive API.
    
    Args:
        file_id: The Google Drive file ID
        output_filename: The local filename to save the file as
        
    Returns:
        True if download successful, False otherwise
    """
    try:
        # 1. Authenticate using WIF token/ADC
        print("Authenticating with Google Cloud...")
        credentials, project = default()

        if not credentials:
            print("FATAL: Could not find credentials. WIF token injection failed.")
            return False

        print(f"Authenticated successfully with project: {project}")

        # 2. Build the Drive service client
        print("Building Google Drive API client...")
        service = build('drive', 'v3', credentials=credentials)

        # 3. Get file metadata first
        print(f"Getting file metadata for ID: {file_id}")
        file_metadata = service.files().get(fileId=file_id).execute()
        print(f"File name: {file_metadata.get('name', 'Unknown')}")
        print(f"File size: {file_metadata.get('size', 'Unknown')} bytes")
        
        # 4. Request file content
        print("Starting file download...")
        request = service.files().get_media(fileId=file_id)
        fh = io.FileIO(output_filename, 'wb')
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        
        while done is False:
            status, done = downloader.next_chunk()
            if status:
                print(f"Download progress: {int(status.progress() * 100)}%")

        print(f"Download successful. Saved to {output_filename}")
        
        # 5. Verify download
        if os.path.exists(output_filename):
            file_size = os.path.getsize(output_filename)
            print(f"Downloaded file size: {file_size} bytes")
            if file_size < 1000:
                print("WARNING: Downloaded file is very small, may be corrupted")
                return False
        else:
            print("ERROR: Downloaded file not found")
            return False
            
        return True

    except Exception as e:
        print(f"Drive API Download failed: {e}")
        print("Please verify:")
        print("1. Service account has access to the file")
        print("2. File ID is correct")
        print("3. WIF provider and service account are correctly configured")
        print("4. Google Drive API is enabled")
        return False


def main():
    """Main function to handle command line arguments and download."""
    if len(sys.argv) != 3:
        print("Usage: python download_database.py <file_id> <output_filename>")
        print("Example: python download_database.py 1r7KSauTJdMnfthq-L2m9q4UjkREm4Byz documentation.zip")
        sys.exit(1)
    
    file_id = sys.argv[1]
    output_filename = sys.argv[2]
    
    print(f"Downloading file ID: {file_id} to: {output_filename}")
    
    success = download_file_from_drive(file_id, output_filename)
    
    if success:
        print("✅ Download completed successfully")
        sys.exit(0)
    else:
        print("❌ Download failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
