spinup
======

`spinup` is a virtual machine manager based on `libvirt`.

A few use cases:

 - Launch a VM with default parameters:

        $ spinup

 - Launch a VM with 4GiB of RAM and 6 CPUs.

        $ spinup 4G 6cpus

 - SSH into a machine spun up in the current directory.

        $ spinup ssh

 - Destroy a VM in the current directory:

        $ spinup destroy

`spinup` is at the moment in its very early stages of development. You
might need to do some setting up before it will work correctly on your
computer.
