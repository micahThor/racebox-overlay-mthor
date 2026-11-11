# First boot of the editor's server (Amazon Linux 2023). Placeholders are filled in by server_stack.py.
set -euxo pipefail
dnf install -y docker
systemctl enable --now docker

# the data volume (videos, settings): wait for it, format it the first time, mount it at /data
VOL=$(echo "__VOLUME_ID__" | tr -d -)
for i in $(seq 1 90); do
  DEV=$(ls /dev/disk/by-id/*"$VOL" 2>/dev/null | head -1 || true)
  [ -n "$DEV" ] && break
  sleep 5
done
[ -n "$DEV" ]
blkid "$DEV" || mkfs -t xfs "$DEV"
mkdir -p /data
grep -q " /data " /etc/fstab || echo "UUID=$(blkid -s UUID -o value "$DEV") /data xfs defaults,nofail 0 2" >> /etc/fstab
mount -a
chown 1000:1000 /data

# the app: pulls the newest image whenever it starts (deploys restart it)
cat > /etc/systemd/system/racebox.service <<'UNIT'
[Unit]
Description=RaceBox Overlay club editor
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service
RequiresMountsFor=/data
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=15
TimeoutStartSec=600
ExecStartPre=-/usr/bin/docker rm -f racebox
ExecStartPre=-/usr/bin/docker image prune -f
ExecStartPre=/bin/sh -c 'aws ecr get-login-password --region __REGION__ | docker login --username AWS --password-stdin __REGISTRY__ && docker pull __IMAGE__ || docker image inspect __IMAGE__ >/dev/null'
ExecStart=/usr/bin/docker run --rm --name racebox --network host -v /data:/data -e RBO_CONFIG_PARAM=__CONFIG_PARAM__ -e AWS_REGION=__REGION__ __IMAGE__ racebox-overlay serve --host 0.0.0.0 --port __PORT__
ExecStop=/usr/bin/docker stop -t 30 racebox

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now racebox.service
