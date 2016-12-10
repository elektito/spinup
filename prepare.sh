#!/bin/bash -e

echo "Creating directory structure..."
sudo mkdir -p /var/lib/spinup/images
sudo chown root:kvm /var/lib/spinup -R
sudo chmod g+w /var/lib/spinup -R

sudo gpasswd -a $(whoami) kvm
array=$(groups)
if [[ "${array[@]}" =~ "libvirtd" ]]; then
    sudo gpasswd -a $(whoami) libvirtd
else
    sudo gpasswd -a $(whoami) libvirt
fi

echo "You might neeed to log out and log in for the group changes to take effect."
echo "Done."

