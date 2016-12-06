#!/usr/bin/env python3

import os
import sys
import subprocess
import yaml
import libvirt
from tempfile import NamedTemporaryFile

def run_cmd(cmd):
    if isinstance(cmd, str):
        cmd = cmd.split(' ')
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, shell=False)
    out, err = proc.communicate()
    return proc.returncode, out, err

def create_cloudconfig_disk():
    with open(os.path.expanduser('~/.ssh/id_rsa.pub')) as f:
        public_key = f.read()

    public_key = public_key.strip()
    public_key = public_key.strip()[public_key.index(' ') + 1:]

    user_data = {}
    meta_data = {
        'instance-id': 'foo',
        'loacl-hostname': 'foo',
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

    #run_cmd('genisoimage -o {} -V cidata -r -J {} {}'.format(
    #    config_iso, user_data_file, meta_data_file))
    run_cmd('cloud-localds {} {} {}'.format(
        config_iso, user_data_file, meta_data_file))

    os.unlink(meta_data_file)
    os.unlink(user_data_file)

    return os.path.abspath(config_iso)

def main():
    libvirt.virEventRegisterDefaultImpl()
    conn = libvirt.open('qemu:///session')
    if conn == None:
        print('Could not connect.', file=sys.stderr)
        exit(1)

    xml = '''
    <domain type='kvm' id='foovm1'>
      <name>foo</name>
      <uuid>a1e08189-8d43-495a-85de-079b14781239</uuid>
      <title>some title</title>
      <description>some description</description>
      <os>
        <type>hvm</type>
      </os>
      <memory unit='MiB'>1024</memory>

      <devices>
        <disk type='file' device='disk'>
          <driver name='qemu' type='qcow2'/>
          <source file='/home/mostafa/Downloads/xenial-server-cloudimg-amd64-disk1.img'/>
          <backingStore/>
          <target dev='vda' bus='virtio'/>
          <alias name='virtio-disk0'/>
          <address type='pci' domain='0x0000' bus='0x00' slot='0x03' function='0x0'/>
        </disk>
        <disk type='file' device='cdrom'>
          <driver name='qemu' type='raw'/>
          <source file='{config_disk}'/>
          <backingStore/>
          <target dev='hda' bus='ide'/>
          <readonly/>
          <alias name='ide0-0-0'/>
          <address type='drive' controller='0' bus='0' target='0' unit='0'/>
        </disk>
        <serial type='pty'>
          <source path='/dev/pts/9'/>
          <target port='0'/>
          <alias name='serial0'/>
        </serial>
        <console type='pty' tty='/dev/pts/9'>
          <source path='/dev/pts/9'/>
          <target type='serial' port='0'/>
          <alias name='serial0'/>
        </console>
        <interface type='bridge'>
          <source bridge='virbr0'/>
          <model type='virtio'/>
        </interface>
      </devices>

    </domain>
    '''

    config_disk = create_cloudconfig_disk()
    xml = xml.format(config_disk=config_disk)

    domain = conn.defineXML(xml)
    domain.create()
    stream = conn.newStream(libvirt.VIR_STREAM_NONBLOCK)
    domain.openConsole(None, stream, 0)

    def stream_callback(stream, events, user_data):
        try:
            received_data = stream.recv(1024)
        except:
            return
        os.write(0, received_data)
    #def stdin_callback(watch, fd, events, unused):
    #    pass
    #stdin_watch = libvirt.virEventAddHandle(0, libvirt.VIR_EVENT_HANDLE_READABLE, stdin_callback, None)
    stream.eventAddCallback(libvirt.VIR_STREAM_EVENT_READABLE, stream_callback, None)

    try:
        while True:
            ret = libvirt.virEventRunDefaultImpl()
            if ret < 0:
                print('Error {}.'.format(ret))
                break
    except KeyboardInterrupt:
        pass
    finally:
        print('Closing connection...')
        conn.close()

if __name__ == '__main__':
    main()
