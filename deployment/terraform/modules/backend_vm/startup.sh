#!/bin/bash

set -e

apt-get update

apt-get install -y \
    gcc \
    ca-certificates \
    curl \
    gnupg

#
# Install Docker
#
install -m 0755 -d /etc/apt/keyrings

curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg

chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) \
  signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update

apt-get install -y \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

#
# Enable Docker service
#
systemctl enable docker
systemctl start docker

#
# Install gcloud CLI (needed for Secret Manager access)
#
apt-get install -y apt-transport-https

curl https://packages.cloud.google.com/apt/doc/apt-key.gpg \
  | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg

echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] \
https://packages.cloud.google.com/apt cloud-sdk main" \
  | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list

apt-get update

apt-get install -y google-cloud-cli

#
# Example Secret Manager fetch
# (uncomment once secrets exist)
#

# DB_PASSWORD=$(gcloud secrets versions access latest \
#   --secret="db-password")

#
# Example env file creation
#

# cat > /opt/backend.env <<EOF
# DB_PASSWORD=$DB_PASSWORD
# EOF

# chmod 600 /opt/backend.env