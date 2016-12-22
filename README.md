spinup
======

`spinup` is a virtual machine manager based on `libvirt`.

A few use cases:

 - Launch a VM with default parameters:

        $ spinup

 - Launch a VM with 4GiB of RAM and 6 CPUs.

        $ spinup 4G 6cpus

 - Launch a CoreOS machine with 2G of RAM:

        $ spinup coreos 2G

 - SSH into a machine spun up in the current directory.

        $ spinup ssh

 - Launch two machines, naming them `foo` and `bar`:

        $ spinup :foo 2G 2cpus -- :bar 8G 4cpus

 - Set disk size:

        $ spinup disk=100G

 - Add two network interfaces, one with a static IP address and one
   configured for DHCP. For the static IP address to work, a libvirt
   network must exist for the given IP range.

        $ spinup 10.3.0.10/24 dhcp

 - Now ssh into `foo`:

        $ spinup ssh foo

 - Destroy the cluster in the current directory:

        $ spinup destroy

`spinup` is at the moment in its very early stages of development. You
might need to do some setting up before it will work correctly on your
computer. The included `prepare.sh` script is supposed to help you do
the one-off work you might need.
