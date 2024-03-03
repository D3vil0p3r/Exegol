from enum import Enum
from typing import Optional, Union, List, Tuple


class DockerDrivers(Enum):
    """Enum for Docker driver type"""
    Disable = "none"
    Host = 'host'
    Bridge = 'bridge'


class ExegolNetworkMode(Enum):
    """Enum for user display"""
    disable = 'Disable'
    host = 'Host'
    docker = 'Docker'
    nat = 'NAT'  # need pre-process
    attached = 'External'  # need pre-process


class ExegolNetwork:
    DEFAULT_DOCKER_NETWORK = [d.value for d in DockerDrivers]

    __DEFAULT_NETWORK_DRIVER = DockerDrivers.Bridge

    def __init__(self, net_mode: ExegolNetworkMode = ExegolNetworkMode.host, net_name: Optional[str] = None):
        self.__net_mode: ExegolNetworkMode = net_mode
        self.__net_name: str = net_name if net_name is not None else net_mode.value
        if self.__net_mode == ExegolNetworkMode.disable:
            self.__docker_net_mode: DockerDrivers = DockerDrivers.Disable
        elif self.__net_mode == ExegolNetworkMode.host:
            self.__docker_net_mode = DockerDrivers.Host
        else:
            self.__docker_net_mode = self.__DEFAULT_NETWORK_DRIVER

    @classmethod
    def instance_network(cls, mode: Union[ExegolNetworkMode, str], container_name: str):
        if type(mode) is str:
            return cls(net_mode=ExegolNetworkMode.attached, net_name=mode)
        else:
            assert type(mode) is ExegolNetworkMode
            if mode in [ExegolNetworkMode.host, ExegolNetworkMode.docker]:
                return cls(net_mode=mode)
            elif mode == ExegolNetworkMode.nat:
                return cls(net_mode=mode, net_name=container_name)
            elif mode == ExegolNetworkMode.disable:
                raise ValueError("Network disable cannot be created")
        raise NotImplementedError("This network type is not implemented yet.")

    @classmethod
    def parse_networks(cls, networks: dict, container_name: str) -> List["ExegolNetwork"]:
        results = []
        for network, config in networks.items():
            try:
                net_mode = ExegolNetworkMode(network)
            except ValueError:
                net_mode = ExegolNetworkMode.nat if network == container_name else ExegolNetworkMode.attached
            results.append(cls(net_mode=net_mode, net_name=network))

        return results

    def getNetworkConfig(self) -> Tuple[str, str]:
        return self.__net_name, self.__docker_net_mode.value

    def getNetworkMode(self) -> ExegolNetworkMode:
        return self.__net_mode

    def getNetworkName(self):
        return self.__net_name

    def getTextNetworkMode(self) -> str:
        if self.__net_mode is ExegolNetworkMode.attached:
            return self.__net_name
        return self.__net_mode.value

    def shouldBeRemoved(self):
        return self.__net_mode == ExegolNetworkMode.nat

    def __repr__(self):
        repr_str = self.__net_mode.value
        if self.__net_mode in [ExegolNetworkMode.nat, ExegolNetworkMode.attached]:
            repr_str += f" ({self.__net_name} : {self.__docker_net_mode.value})"
        return repr_str
