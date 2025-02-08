#!/bin/bash

dnf remove update-motd -y
rm -rf /etc/motd
rm -rf /etc/motd.d
rm /usr/lib/motd
rm -rf /usr/lib/motd.d

# SETUP MY TOOLS

dnf -y install htop wget gcc make git telnet python3.11 java-22-amazon-corretto-devel

## Setup BTOP - not available via dnf or amazon-linux-extras
mkdir /home/ec2-user/bin
wget https://github.com/aristocratos/btop/releases/download/v1.4.0/btop-x86_64-linux-musl.tbz
tar xjf btop-x86_64-linux-musl.tbz
mv btop/bin/btop /home/ec2-user/bin
rm -rf btop-x86_64-linux-musl.tbz btop
chown -R ec2-user /home/ec2-user/bin
chmod -R 755 /home/ec2-user/bin

#DOWNLOAD REDIS

wget --quiet https://download.redis.io/releases/redis-5.0.10.tar.gz
tar xzf redis-5.0.10.tar.gz
rm redis-5.0.10.tar.gz
mv redis-5.0.10 /home/ec2-user
ln -s /home/ec2-user/redis-5.0.10 /home/ec2-user/redis
echo "export PATH=\$PATH:/home/ec2-user/redis/src" >> /home/ec2-user/.bashrc
cd /home/ec2-user/redis/deps
make --quiet hiredis jemalloc linenoise lua geohash-int > /dev/null 2>&1
cd /home/ec2-user/redis
make --quiet > /dev/null 2>&1
chown -R ec2-user /home/ec2-user/redis*

# Update Python, PIP. Install lolcat for banner

sudo -u ec2-user -i bash -c '\
python3 -m ensurepip --upgrade; \
python3 -m pip install -q --upgrade pip; \
pip3 install -q --upgrade pip; \
pip3 install -q lolcat; \
pip3 install -q pyfiglet;'

# SETUP MY BASH CUSTOMIZATIONS AND PRETTIFY WITH LOLCAT

cat << 'EOF' >> /home/ec2-user/.bashrc

get_token() {
    curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600"
    echo
}

ec2name() {
    curl -s -H "X-aws-ec2-metadata-token: `get_token`" http://169.254.169.254/latest/meta-data/tags/instance/Name
    echo
}

refresh_banner() {
    echo > /home/ec2-user/.banner.txt
    pyfiglet -f ansi_shadow $(ec2name) >> /home/ec2-user/.banner.txt
}

alias python3='python3.11'
alias python='python3.11'
alias c='cd ..'
set -o vi

refresh_banner

export PATH=/home/ec2-user/bin:$PATH
export PATH=/home/ec2-user/.local/bin:$PATH

## Always keep lolcat after this check and at the end or it messes up SCP
[[ $- == *i* ]] || return
lolcat /home/ec2-user/.banner.txt

EOF


cat << 'EOF' >> /home/ec2-user/redis/primary.conf
port 6379
cluster-enabled yes
cluster-config-file cluster.conf
cluster-node-timeout 5000
appendonly yes
protected-mode no

EOF

chown ec2-user /home/ec2-user/redis/primary.conf

cat << 'EOF' >> /home/ec2-user/redis/replica.conf
replica-read-only yes
port 6379
cluster-enabled yes
cluster-config-file cluster.conf
cluster-node-timeout 5000
appendonly yes
protected-mode no

EOF

chown ec2-user /home/ec2-user/redis/replica.conf

chown ec2-user /home/ec2-user/.bashrc /home/ec2-user/.banner.txt
