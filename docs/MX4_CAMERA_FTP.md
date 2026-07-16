# MX4 Camera FTP Ingest

- Source host: `ass-proxmox-linux`
- Host tailnet IP: `100.124.55.22`
- Host LAN IP used by cameras: `192.168.1.2`
- FTP listener: `192.168.1.2:21`
- Camera data root: `/home/aurora/data/mx4`
- FTP chroot root: `/srv/ftp/mx`
- FTP data bind mount: `/srv/ftp/mx/mx4` -> `/home/aurora/data/mx4`

This is a source-side camera ingest on the ASS Linux VM. It is separate from
the existing SSH/SFTP-based CL61 source path and does not change
`/home/aurora/data/cl61`.

## Cameras

The four MOBOTIX M24 cameras upload one QXGA JPEG every 60 seconds using the
shared FTP user `mx`.

| Camera | IP | FTP directory template | File name template | Host landing path |
| --- | --- | --- | --- | --- |
| `end-south-array-cam` | `192.168.1.27` | `mx4/end-south-array-cam/$(TMS.DATE)` | `end-south-array-cam_$(TMS.DATE)_$(TMS.HOUR)-$(TMS.MIN).jpg` | `/home/aurora/data/mx4/end-south-array-cam/YYYY-MM-DD/end-south-array-cam_YYYY-MM-DD_HH-MM.jpg` |
| `fence-post-cam` | `192.168.1.28` | `mx4/fence-post-cam/$(TMS.DATE)` | `fence-post-cam_$(TMS.DATE)_$(TMS.HOUR)-$(TMS.MIN).jpg` | `/home/aurora/data/mx4/fence-post-cam/YYYY-MM-DD/fence-post-cam_YYYY-MM-DD_HH-MM.jpg` |
| `radar-cam` | `192.168.1.29` | `mx4/radar-cam/$(TMS.DATE)` | `radar-cam_$(TMS.DATE)_$(TMS.HOUR)-$(TMS.MIN).jpg` | `/home/aurora/data/mx4/radar-cam/YYYY-MM-DD/radar-cam_YYYY-MM-DD_HH-MM.jpg` |
| `mid-south-array-cam` | `192.168.1.30` | `mx4/mid-south-array-cam/$(TMS.DATE)` | `mid-south-array-cam_$(TMS.DATE)_$(TMS.HOUR)-$(TMS.MIN).jpg` | `/home/aurora/data/mx4/mid-south-array-cam/YYYY-MM-DD/mid-south-array-cam_YYYY-MM-DD_HH-MM.jpg` |

Legacy test uploads and the earlier `$(TMS.YDAY)-$(TMS.HOUR)-$(TMS.MIN)-$(TMS.SEC).jpg`
files were deleted on `2026-07-07` after the new filename template was verified
on all four cameras.

## FTP Server Configuration

`vsftpd` is configured for camera ingest only:

- listens on `192.168.1.2:21`
- local login enabled for allow-listed user `mx`
- anonymous FTP disabled
- shared FTP user `mx` has `/usr/sbin/nologin`
- `mx` is chrooted to `/srv/ftp/mx`
- passive ports remain available as `40000-40100`

The camera-side connection mode is Active FTP. Passive FTP remains configured on
the server for troubleshooting, but is not the normal camera setting.

The generated FTP password is not stored in this repository. On
`ass-proxmox-linux`, it is stored for the `aurora` user at:

```text
/home/aurora/.config/mx4-ftp/ftp_password
```

Pre-change host config backups were left on `ass-proxmox-linux` with names like:

```text
/etc/vsftpd.conf.codex-pre-mx4-*
/etc/fstab.codex-pre-mx4-*
/etc/ftpusers.codex-pre-mx4-*
```

## Camera FTP Settings

Each MOBOTIX camera uses:

```text
FTP Server:       192.168.1.2
User Name:        mx
Connection:       Active FTP
FTP Upload:       Ftpupdate
Update Interval:  60 s
Time Table:       No time table
Low-Light Cut-Off: Off
File Type:        Image
Image Profile:    QXGA
File Name:        <camera-name>_$(TMS.DATE)_$(TMS.HOUR)-$(TMS.MIN).jpg
```

After any camera-side change, click `Set` and then store the current camera
configuration permanently in flash memory.

## Exposure Settings

The MOBOTIX exposure page is configured identically on all four cameras:

```text
Automatic Contrast:          Extended
Brightness:                  0
Backlight Correction:        4
Exposure Control:            Full Image Area
Exposure Windows:            Full Image
Exposure Weighting:          100%
White Balance Weighting:     Total
Show Windows:                Off
Target Brightness:           25%
Minimum Average Brightness:  Off
Night Improvement:           Off
Exposure Program:            0
Max. Exposure Time:          Max 1/30
Min. Exposure Time:          Min 1/8000
Frequency of Power Supply:   50 Hz
```

These settings favor a consistent archive while allowing more night light than a
`1/90` cap. If night images are still too dark, first review custom exposure
windows for each camera before enabling night improvement.

## Verification

Check the FTP service and mount:

```bash
ssh aurora@100.124.55.22 'systemctl is-active vsftpd; ss -ltn sport = :21; findmnt -rn --target /srv/ftp/mx/mx4'
```

Check recent uploads:

```bash
ssh aurora@100.124.55.22 'find /home/aurora/data/mx4 -maxdepth 3 -type f -name "*.jpg" -mmin -5 -printf "%TY-%Tm-%Td %TH:%TM:%TS %p\n" | sort'
```

Check camera-side FTP logs:

```bash
ssh aurora@100.124.55.22 'tail -50 /var/log/vsftpd.log'
```

Confirm CL61 SFTP remains available:

```bash
printf 'ls /home/aurora/data/cl61\nbye\n' | sftp -b - aurora@100.124.55.22
```

On `2026-07-07`, all four cameras accepted the settings, stored the
configuration, and uploaded fresh JPEGs into their per-camera day folders with
the `camera-name_YYYY-MM-DD_HH-MM.jpg` filename format.
