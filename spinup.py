#!/usr/bin/env python3

import os
import subprocess
import yaml
from tempfile import NamedTemporaryFile

images = {
    'ubuntu': 'xenial-server-cloudimg-amd64-disk1.img'
}

def run_cmd(cmd):
    if isinstance(cmd, str):
        cmd = cmd.split(' ')
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, shell=False)
    out, err = proc.communicate()
    return proc.returncode, out, err

def main():
    name = 'foovm0'
    description = 'spinup-spinned vm'
    image = 'ubuntu'
    os_type = 'linux'
    os_variant = 'ubuntu16.04'
    memory = 1024
    cpus = 2
    disk_file = '/var/lib/libvirt/images/' + images[image]

    with open(os.path.expanduser('~/.ssh/id_rsa.pub')) as f:
        public_key = f.read()

    public_key = public_key.strip()
    public_key = public_key.strip()[public_key.index(' ') + 1:]

    user_data = {}
    meta_data = {
        'instance-id': name,
        'loacl-hostname': name,
        'public-keys': {
            'ssh-rsa': public_key,
        },
    }

    config_iso = '.config.iso'

    with NamedTemporaryFile(mode='w', delete=False) as f:
        yaml.dump(meta_data, f)
        meta_data_file = f.name

    with NamedTemporaryFile(mode='w', delete=False) as f:
        yaml.dump(user_data, f)
        user_data_file = f.name

    run_cmd('genisoimage -o {} -V cidata -r -J {} {}'.format(
        config_iso, user_data_file, meta_data_file))

    code, out, err = run_cmd(['virt-install',
             '-n', name,
             '--os-type', os_type,
             '--os-variant', os_variant,
             '--ram', str(memory),
             '--vcpus', str(cpus),
             '--graphics', 'none',
             '--import',
             '--disk', 'path={}'.format(disk_file),
             '--disk', 'path={},device=cdrom'.format(config_iso)])

    if code != 0:
        print('Error creating VM.')
        if err:
            print(err.decode())
        elif out:
            print(out.decode())

if __name__ == '__main__':
    main()
