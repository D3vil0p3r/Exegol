import os
from datetime import datetime
from time import sleep
from typing import List, Optional, Union, cast

import docker
from docker import DockerClient
from docker.errors import APIError, DockerException, NotFound, ImageNotFound
from docker.models.images import Image
from docker.models.networks import Network
from docker.models.volumes import Volume
from docker.types import IPAMPool, IPAMConfig
from requests import ReadTimeout

from exegol.config.ConstantConfig import ConstantConfig
from exegol.config.DataCache import DataCache
from exegol.config.EnvInfo import EnvInfo
from exegol.config.UserConfig import UserConfig
from exegol.console.TUI import ExegolTUI
from exegol.console.cli.ParametersManager import ParametersManager
from exegol.exceptions.ExegolExceptions import ObjectNotFound
from exegol.model.ExegolContainer import ExegolContainer
from exegol.model.ExegolContainerTemplate import ExegolContainerTemplate
from exegol.model.ExegolImage import ExegolImage
from exegol.model.ExegolNetwork import ExegolNetwork
from exegol.model.MetaImages import MetaImages
from exegol.utils.ExeLog import logger, console, ExeLog
from exegol.utils.MetaSingleton import MetaSingleton
from exegol.utils.NetworkUtils import NetworkUtils
from exegol.utils.WebUtils import WebUtils


# SDK Documentation : https://docker-py.readthedocs.io/en/stable/index.html


class DockerUtils(metaclass=MetaSingleton):

    def __init__(self):
        """Utility class between exegol and the Docker SDK"""
        try:
            # Connect Docker SDK to the local docker instance.
            # Docker connection setting is loaded from the user environment variables.
            self.__client: DockerClient = docker.from_env()
            # Check if the docker daemon is serving linux container
            self.__daemon_info = self.__client.info()
            if self.__daemon_info.get("OSType", "linux").lower() != "linux":
                logger.critical(
                    f"Docker daemon is not serving linux container ! Docker OS Type is: {self.__daemon_info.get('OSType', 'linux')}")
            EnvInfo.initData(self.__daemon_info)
        except DockerException as err:
            if 'ConnectionRefusedError' in str(err):
                logger.critical(f"Unable to connect to docker (from env config). Is docker running on your machine? Exiting.{os.linesep}"
                                f"    Check documentation for help: https://exegol.readthedocs.io/en/latest/getting-started/faq.html#unable-to-connect-to-docker")
            elif 'FileNotFoundError' in str(err):
                logger.critical(f"Unable to connect to docker. Is docker installed on your machine? Exiting.{os.linesep}"
                                f"    Check documentation for help: https://exegol.readthedocs.io/en/latest/getting-started/faq.html#unable-to-connect-to-docker")
            elif 'PermissionError' in str(err):
                logger.critical(f"Docker is installed on your host but you don't have the permission to interact with it. Exiting.{os.linesep}"
                                f"    Check documentation for help: https://exegol.readthedocs.io/en/latest/getting-started/install.html#optional-run-exegol-with-appropriate-privileges")
            else:
                logger.error(err)
                logger.critical(
                    "Unable to connect to docker (from env config). Is docker operational and accessible? on your machine? "
                    "Exiting.")
        self.__images: Optional[List[ExegolImage]] = None
        self.__containers: Optional[List[ExegolContainer]] = None

    def clearCache(self):
        """Remove class's images and containers data cache
        Only needed if the list has to be updated in the same runtime at a later moment"""
        self.__containers = None
        self.__images = None

    def getDockerInfo(self) -> dict:
        """Fetch info from docker daemon"""
        return self.__daemon_info

    # # # Container Section # # #

    def listContainers(self) -> List[ExegolContainer]:
        """List available docker containers.
        Return a list of ExegolContainer"""
        if self.__containers is None:
            self.__containers = []
            try:
                docker_containers = self.__client.containers.list(all=True, filters={"name": "exegol-"})
            except APIError as err:
                logger.debug(err)
                logger.critical(err.explanation)
                # Not reachable, critical logging will exit
                return  # type: ignore
            except ReadTimeout:
                logger.critical("Received a timeout error, Docker is busy... Unable to list containers, retry later.")
                return  # type: ignore
            for container in docker_containers:
                self.__containers.append(ExegolContainer(container))
        return self.__containers

    def createContainer(self, model: ExegolContainerTemplate, temporary: bool = False) -> ExegolContainer:
        """Create an Exegol container from an ExegolContainerTemplate configuration.
        Return an ExegolContainer if the creation was successful."""
        logger.info("Creating new exegol container")
        model.prepare()
        logger.debug(model)
        # Preload docker volume before container creation
        for volume in model.config.getVolumes():
            if volume.get('Type', '?') == "volume":
                docker_volume = self.__loadDockerVolume(volume_path=volume['Source'], volume_name=volume['Target'])
                if docker_volume is None:
                    logger.warning(f"Error while creating docker volume '{volume['Target']}'")
        entrypoint, command = model.config.getEntrypointCommand()
        logger.debug(f"Entrypoint: {entrypoint}")
        logger.debug(f"Cmd: {command}")
        # The 'create' function must be called to create a container without starting it
        # in order to hot patch the entrypoint.sh with wrapper features (the container will be started after postCreateSetup)
        docker_create_function = self.__client.containers.create
        docker_args = {"image": model.image.getDockerRef(),
                       "entrypoint": entrypoint,
                       "command": command,
                       "detach": True,
                       "name": model.getContainerName(),
                       "hostname": model.config.hostname,
                       "extra_hosts": model.config.getExtraHost(),
                       "devices": model.config.getDevices(),
                       "environment": model.config.getEnvs(),
                       "labels": model.config.getLabels(),
                       "ports": model.config.getPorts(),
                       "privileged": model.config.getPrivileged(),
                       "cap_add": model.config.getCapabilities(),
                       "sysctls": model.config.getSysctls(),
                       "shm_size": model.config.shm_size,
                       "stdin_open": model.config.interactive,
                       "tty": model.config.tty,
                       "mounts": model.config.getVolumes(),
                       "working_dir": model.config.getWorkingDir()}
        # Add networking args
        if model.config.isNetworkDisabled():
            docker_args["network_disabled"] = True
        else:
            docker_args["network"], network_driver = model.config.getNetwork()
            if (docker_args["network"] is not None and
                    network_driver is not None and
                    not self.networkExist(docker_args["network"])):
                if not self.createNetwork(network_name=docker_args["network"], driver=network_driver):
                    logger.critical("Unable to create the dedicated network for the new container. Aborting.")

        # Handle temporary arguments
        if temporary:
            # Only the 'run' function support the "remove" parameter
            docker_create_function = self.__client.containers.run
            docker_args["remove"] = temporary
            docker_args["auto_remove"] = temporary

        # Create container
        try:
            container = docker_create_function(**docker_args)
        except APIError as err:
            message = err.explanation.decode('utf-8').replace('[', '\\[') if type(err.explanation) is bytes else err.explanation
            if message is not None:
                message = message.replace('[', '\\[')
                logger.error(f"Docker error received: {message}")
            logger.debug(err)
            model.rollback()
            try:
                container = self.__client.containers.list(all=True, filters={"name": model.getContainerName()})
                if container is not None and len(container) > 0:
                    for c in container:
                        if c.name == model.getContainerName():  # Search for exact match
                            container[0].remove()
                            logger.debug("Container removed")
            except Exception:
                pass
            try:
                if self.removeNetwork(docker_args["network"]):
                    logger.debug("Network removed")
            except Exception:
                pass
            logger.critical("Error while creating exegol container. Exiting.")
            # Not reachable, critical logging will exit
            return  # type: ignore
        if container is not None:
            logger.success("Exegol container successfully created !")
        else:
            logger.critical("Unknown error while creating exegol container. Exiting.")
            # Not reachable, critical logging will exit
            return  # type: ignore
        return ExegolContainer(container, model)

    def getContainer(self, tag: str) -> ExegolContainer:
        """Get an ExegolContainer from tag name."""
        try:
            # Fetch potential container match from DockerSDK
            container = self.__client.containers.list(all=True, filters={"name": f"exegol-{tag}"})
        except APIError as err:
            logger.debug(err)
            logger.critical(err.explanation)
            # Not reachable, critical logging will exit
            return  # type: ignore
        # Check if there is at least 1 result. If no container was found, raise ObjectNotFound.
        if container is None or len(container) == 0:
            # Handle case-insensitive OS
            if EnvInfo.isWindowsHost() or EnvInfo.isMacHost():
                # First try to fetch the container as-is (for retroactive support with old container with uppercase characters)
                # If the user's input didn't match any container, try to force the name in lowercase if not already tried
                lowered_tag = tag.lower()
                if lowered_tag != tag:
                    return self.getContainer(lowered_tag)
            raise ObjectNotFound
        # Filter results with exact name matching
        for c in container:
            if c.name == f"exegol-{tag}":
                # When the right container is found, select it and stop the search
                return ExegolContainer(c)
        # When there is some close container's name,
        # docker may return some results but none of them correspond to the request.
        # In this case, ObjectNotFound is raised
        raise ObjectNotFound

    # # # Volumes Section # # #

    def __loadDockerVolume(self, volume_path: str, volume_name: str) -> Volume:
        """Load or create a docker volume for exegol containers
        (must be created before the container, SDK limitation)
        Return the docker volume object"""
        try:
            os.makedirs(volume_path, exist_ok=True)
        except PermissionError:
            logger.error("Unable to create the volume folder on the filesystem locally.")
            logger.critical(f"Insufficient permission to create the folder: {volume_path}")
        try:
            # Check if volume already exist
            volume = self.__client.volumes.get(volume_name)
            path = volume.attrs.get('Options', {}).get('device', '')
            if path != volume_path:
                try:
                    self.__client.api.remove_volume(name=volume_name)
                    raise NotFound('Volume must be reloaded')
                except APIError as e:
                    if e.status_code == 409:
                        logger.warning("The path of the volume specified by the user is not the same as in the existing docker volume. "
                                       "The user path will be [red]ignored[/red] as long as the docker volume already exists.")
                        logger.verbose("The volume is already used by some container and cannot be automatically removed.")
                        logger.debug(e.explanation)
                    else:
                        raise NotFound('Volume must be reloaded')
                except ReadTimeout:
                    logger.error(f"Received a timeout error, Docker is busy... Volume {volume_name} cannot be automatically removed. Please, retry later the following command:{os.linesep}"
                                 f"    [orange3]docker volume rm {volume_name}[/orange3]")
        except NotFound:
            try:
                # Creating a docker volume bind to a host path
                # Docker volume are more easily shared by container
                # Docker volume can load data from container image on host's folder creation
                volume = self.__client.volumes.create(volume_name, driver="local",
                                                      driver_opts={'o': 'bind',
                                                                   'device': volume_path,
                                                                   'type': 'none'})
            except APIError as err:
                logger.error(f"Error while creating docker volume '{volume_name}'.")
                logger.debug(err)
                logger.critical(err.explanation)
                return None  # type: ignore
            except ReadTimeout:
                logger.critical(f"Received a timeout error, Docker is busy... Volume {volume_name} cannot be created.")
                return  # type: ignore
        except APIError as err:
            logger.critical(f"Unexpected error by Docker SDK : {err}")
            return None  # type: ignore
        except ReadTimeout:
            logger.critical("Received a timeout error, Docker is busy... Unable to enumerate volume, retry later.")
            return None  # type: ignore
        return volume

    # # # Network Section # # #

    def listAttachableNetworks(self) -> List[Network]:
        """List every non-default networks"""
        networks = []
        try:
            networks = self.__client.networks.list()
        except APIError as e:
            raise e
        except ReadTimeout:
            logger.critical("Received a timeout error, Docker is busy... Unable to enumerate volume, retry later.")
        for net in networks.copy():
            if net.name in ExegolNetwork.DEFAULT_DOCKER_NETWORK:
                networks.remove(net)
        return networks

    def listExegolNetworks(self) -> List[Network]:
        """List every exegol networks"""
        networks = []
        try:
            networks = self.__client.networks.list(filters={"label": "source=exegol"})
        except APIError as e:
            raise e
        except ReadTimeout:
            logger.critical("Received a timeout error, Docker is busy... Unable to enumerate volume, retry later.")
        return networks

    def getNetwork(self, network_name: str, exegol_only: bool = False) -> Optional[Network]:
        """Find a specific network"""
        networks: List[Network] = []
        filter = {}
        if exegol_only:
            filter["label"] = "source=exegol"
        try:
            networks = self.__client.networks.list(names=network_name, filters=filter)
        except APIError as e:
            raise e
        except ReadTimeout:
            logger.critical("Received a timeout error, Docker is busy... Unable to enumerate volume, retry later.")
        for net in networks:
            # Search for an exact match
            if net.name == network_name:
                return net
        return None

    def networkExist(self, network_name: str) -> bool:
        """Return True is the supplied network name exist"""
        return self.getNetwork(network_name) is not None

    def createNetwork(self, network_name: str, driver: str) -> bool:
        """Create a new exegol network"""
        ip_pool = IPAMPool(subnet=str(NetworkUtils.get_next_available_range(UserConfig().network_dedicated_range, UserConfig().network_default_netmask)))
        config = IPAMConfig(pool_configs=[ip_pool])
        try:
            network: Network = self.__client.networks.create(name=network_name, driver=driver, labels={"source": "exegol"}, check_duplicate=True, ipam=config)
            return True
        except APIError as e:
            if e.status_code == 409:
                logger.error("This network already exist.")
                return False
            raise e
        except ReadTimeout:
            logger.critical("Received a timeout error, Docker is busy... Unable to enumerate volume, retry later.")
        return False

    def removeNetwork(self, network_name: Optional[str] = None, network: Optional[Network] = None) -> bool:
        """Remove exegol network"""
        if network is None:
            if network_name is None:
                raise ValueError("One of the parameter must be supplied.")
            elif network_name in ExegolNetwork.DEFAULT_DOCKER_NETWORK:
                # Default docker driver cannot be deleted
                return False
            network = self.getNetwork(network_name, exegol_only=True)

        if network is not None:
            if network.name in ExegolNetwork.DEFAULT_DOCKER_NETWORK:
                # Default docker driver cannot be deleted
                return False
            try:
                network.remove()
                logger.success(f"Dedicated network successfully removed.")
                return True
            except NotFound:
                logger.verbose(f"The dedicated network {network.name} was already removed.")
            except APIError as e:
                logger.error(f"The associated dedicated network cannot be automatically removed. "
                             f"You have to delete it manually ({network.name}). Error: {e.explanation}")
        else:
            logger.info(f"The network {network_name} will not be deleted. Only exegol network can be automatically deleted.")
        return False

    # # # Image Section # # #

    def listImages(self, include_version_tag: bool = False, include_locked: bool = False) -> List[ExegolImage]:
        """List available docker images.
        Return a list of ExegolImage"""
        if self.__images is None:
            remote_images = self.__listRemoteImages()
            local_images = self.__listLocalImages()
            self.__images = ExegolImage.mergeImages(remote_images, local_images)
        result = self.__images
        assert result is not None
        # Caching latest images
        DataCache().update_image_cache([img for img in result if not img.isVersionSpecific()])
        if not (logger.isEnabledFor(ExeLog.VERBOSE) or include_locked):
            # ToBeRemoved images are only shown in verbose mode
            result = [i for i in result if not i.isLocked()]
        if not include_version_tag:
            # Version specific images not installed are excluded by default
            result = [img for img in result if not img.isVersionSpecific() or img.isInstall()]
        return result

    def listInstalledImages(self) -> List[ExegolImage]:
        """List installed docker images.
        Return a list of ExegolImage"""
        images = self.listImages()
        # Selecting only installed image
        return [img for img in images if img.isInstall()]

    def getImage(self, tag: str) -> ExegolImage:
        """Get an ExegolImage from tag name."""
        # Fetch every images available
        images = self.listImages(include_version_tag=True, include_locked=True)
        match: Optional[ExegolImage] = None
        # Find a match
        for i in images:
            if i.getName() == tag:
                # If there is a locked image keep it as default
                if i.isLocked():
                    match = i
                else:
                    # Return the first non-outdated image
                    return i
        # If there is any match without lock (outdated) status, return the last outdated image found.
        if match is not None:
            return match
        # If there is no match at all, raise ObjectNotFound to handle the error
        raise ObjectNotFound

    def getInstalledImage(self, tag: str) -> ExegolImage:
        """Get an already installed ExegolImage from tag name."""
        try:
            if self.__images is None:
                try:
                    docker_local_image = self.__client.images.get(f"{ConstantConfig.IMAGE_NAME}:{tag}")
                    # DockerSDK image get is an exact matching, no need to add more check
                except APIError as err:
                    if err.status_code == 404:
                        # try to find it in recovery mode
                        logger.verbose("Unable to find your image. Trying to find in recovery mode.")
                        recovery_images = self.__findLocalRecoveryImages(include_untag=True)
                        match = []
                        for img in recovery_images:
                            if ExegolImage.parseAliasTagName(img) == tag:
                                match.append(ExegolImage(docker_image=img))
                        if len(match) == 1:
                            return match[0]
                        elif len(match) > 1:
                            return cast(ExegolImage, ExegolTUI.selectFromTable(match))
                        raise ObjectNotFound
                    else:
                        logger.critical(f"Error on image loading: {err}")
                        return  # type: ignore
                except ReadTimeout:
                    logger.critical("Received a timeout error, Docker is busy... Unable to list images, retry later.")
                    return  # type: ignore
                return ExegolImage(docker_image=docker_local_image).autoLoad()
            else:
                for img in self.__images:
                    if img.getName() == tag:
                        if not img.isInstall() or not img.isUpToDate():
                            # Refresh local image status in case of installation/upgrade operations
                            self.__findImageMatch(img)
                        return img
        except ObjectNotFound:
            logger.critical(f"The desired image is not installed or do not exist ({ConstantConfig.IMAGE_NAME}:{tag}). Exiting.")
        return  # type: ignore

    def __listLocalImages(self, tag: Optional[str] = None) -> List[Image]:
        """List local docker images already installed.
        Return a list of docker images objects"""
        logger.debug("Fetching local image tags, digests (and other attributes)")
        try:
            image_name = ConstantConfig.IMAGE_NAME + ("" if tag is None else f":{tag}")
            images = self.__client.images.list(image_name, filters={"dangling": False})
        except APIError as err:
            logger.debug(err)
            logger.critical(err.explanation)
            # Not reachable, critical logging will exit
            return  # type: ignore
        except ReadTimeout:
            logger.critical("Received a timeout error, Docker is busy... Unable to list local images, retry later.")
            return  # type: ignore
        # Filter out image non-related to the right repository
        result = []
        ids = set()
        for img in images:
            # len tags = 0 handle exegol <none> images (nightly image lost their tag after update)
            if len(img.attrs.get('RepoTags', [])) == 0 or \
                    ConstantConfig.IMAGE_NAME in [repo_tag.split(':')[0] for repo_tag in img.attrs.get("RepoTags", [])]:
                result.append(img)
                ids.add(img.id)

        # Try to find lost Exegol images
        recovery_images = self.__findLocalRecoveryImages()
        for img in recovery_images:
            # Docker can keep track of 2 images maximum with RepoTag or RepoDigests, after it's hard to track origin without labels, so this recovery option is "best effort"
            if img.id in ids:
                # Skip image from other repo and image already found
                logger.debug(f"Duplicate found in recovery mode! {img}")
                continue
            else:
                result.append(img)
                ids.add(img.id)
        return result

    def __findLocalRecoveryImages(self, include_untag: bool = False) -> List[Image]:
        """This method try to recovery untagged docker images.
        Set include_untag option to recover images with a valid RepoDigest (no not dangling) but without tag."""
        try:
            # Try to find lost Exegol images
            recovery_images = self.__client.images.list(filters={"dangling": True})
            if include_untag:
                recovery_images += self.__client.images.list(ConstantConfig.IMAGE_NAME, filters={"dangling": False})
        except APIError as err:
            logger.debug(f"Error occurred in recovery mode: {err}")
            return []
        except ReadTimeout:
            logger.critical("Received a timeout error, Docker is busy... Unable to enumerate lost images, retry later.")
            return  # type: ignore
        result = []
        id_list = set()
        for img in recovery_images:
            # Docker can keep track of 2 images maximum with RepoTag or RepoDigests, after it's hard to track origin without labels, so this recovery option is "best effort"
            repo_tags = img.attrs.get('RepoTags')
            repo_digest = img.attrs.get('RepoDigests')
            if repo_tags is not None and len(repo_tags) > 0 or (not include_untag and repo_digest is not None and len(repo_digest) > 0) or img.id in id_list:
                # Skip image from other repo and image already found
                continue
            if img.labels.get('org.exegol.app', '') == "Exegol":
                result.append(img)
                id_list.add(img.id)
        return result

    def __listRemoteImages(self) -> List[MetaImages]:
        """List remote dockerhub images available.
        Return a list of ExegolImage"""
        logger.debug("Fetching remote image tags, digests and sizes")
        remote_results = []
        # Define max number of tags to download from dockerhub (in order to limit download time and discard historical versions)
        page_size = 20
        page_max = 2
        current_page = 0
        url: Optional[str] = f"https://{ConstantConfig.DOCKER_HUB}/v2/repositories/{ConstantConfig.IMAGE_NAME}/tags?page_size={page_size}"
        # Handle multi-page tags from registry
        with console.status(f"Loading registry information from [green]{url}[/green]", spinner_style="blue") as s:
            while url is not None:
                if current_page == page_max:
                    logger.debug("Max page limit reached. In non-verbose mode, downloads will stop there.")
                    if not logger.isEnabledFor(ExeLog.VERBOSE):
                        break
                current_page += 1
                logger.debug(f"Fetching information from: {url}")
                s.update(status=f"Fetching registry information from [green]{url}[/green]")
                docker_repo_response = WebUtils.runJsonRequest(url, "Dockerhub")
                if docker_repo_response is None:
                    logger.warning("Skipping online queries.")
                    return []
                error_message = docker_repo_response.get("message")
                if error_message:
                    logger.error(f"Dockerhub send an error message: {error_message}")
                for docker_images in docker_repo_response.get("results", []):
                    meta_image = MetaImages(docker_images)
                    remote_results.append(meta_image)
                url = docker_repo_response.get("next")  # handle multiple page tags
        # Remove duplication (version specific / latest release)
        return remote_results

    def __findImageMatch(self, remote_image: ExegolImage):
        """From a Remote ExegolImage, try to find a local match (using Remote DigestID).
        This method is useful if the image repository name is also lost"""
        remote_id = remote_image.getLatestRemoteId()
        if not remote_id:
            logger.debug("Latest remote id is not available... Falling back to the current remote id.")
            remote_id = remote_image.getRemoteId()
        try:
            docker_image = self.__client.images.get(f"{ConstantConfig.IMAGE_NAME}@{remote_id}")
        except ImageNotFound:
            raise ObjectNotFound
        except ReadTimeout:
            logger.critical("Received a timeout error, Docker is busy... Unable to find a specific image, retry later.")
            return  # type: ignore
        remote_image.resetDockerImage()
        remote_image.setDockerObject(docker_image)

    def downloadImage(self, image: ExegolImage, install_mode: bool = False) -> bool:
        """Download/pull an ExegolImage"""
        if ParametersManager().offline_mode:
            logger.critical("It's not possible to download a docker image in offline mode ...")
            return False
        # Switch to install mode if the selected image is not already installed
        install_mode = install_mode or not image.isInstall()
        logger.info(f"{'Installing' if install_mode else 'Updating'} exegol image : {image.getName()}")
        name = image.updateCheck()
        if name is not None:
            logger.info(f"Pulling compressed image, starting a [cyan1]~{image.getDownloadSize()}[/cyan1] download :satellite:")
            logger.info(f"Once downloaded and uncompressed, the image will take [cyan1]~{image.getRealSizeRaw()}[/cyan1] on disk :floppy_disk:")
            logger.debug(f"Downloading {ConstantConfig.IMAGE_NAME}:{name} ({image.getArch()})")
            try:
                ExegolTUI.downloadDockerLayer(
                    self.__client.api.pull(repository=ConstantConfig.IMAGE_NAME,
                                           tag=name,
                                           stream=True,
                                           decode=True,
                                           platform="linux/" + image.getArch()))
                logger.success(f"Image successfully {'installed' if install_mode else 'updated'}")
                # Remove old image
                if not install_mode and image.isInstall() and UserConfig().auto_remove_images:
                    self.removeImage(image, upgrade_mode=not install_mode)
                return True
            except APIError as err:
                if err.status_code == 500:
                    logger.error(f"Error: {err.explanation}")
                    logger.error(f"Error while contacting docker registry. Aborting.")
                elif err.status_code == 404:
                    logger.critical(f"The image has not been found on the docker registry: {err.explanation}")
                else:
                    logger.debug(f"Error: {err}")
                    logger.critical(f"An error occurred while downloading this image: {err.explanation}")
            except ReadTimeout:
                logger.critical(f"Received a timeout error, Docker is busy... Unable to download {name} image, retry later.")
        return False

    def downloadVersionTag(self, image: ExegolImage) -> Union[ExegolImage, str]:
        """Pull a docker image for a specific version tag and return the corresponding ExegolImage"""
        if ParametersManager().offline_mode:
            logger.critical("It's not possible to download a docker image in offline mode ...")
            return ""
        try:
            image = self.__client.images.pull(repository=ConstantConfig.IMAGE_NAME,
                                              tag=image.getLatestVersionName(),
                                              platform="linux/" + image.getArch())
            return ExegolImage(docker_image=image, isUpToDate=True)
        except APIError as err:
            if err.status_code == 500:
                return f"error while contacting docker registry: {err.explanation}"
            elif err.status_code == 404:
                return f"matching tag doesn't exist: {err.explanation}"
            else:
                logger.debug(f"Error: {err}")
                return f"en unknown error occurred while downloading this image : {err.explanation}"
        except ReadTimeout:
            logger.critical(f"Received a timeout error, Docker is busy... Unable to download an image tag, retry later the following command:{os.linesep}"
                            f"    [orange3]docker pull --platform linux/{image.getArch()} {ConstantConfig.IMAGE_NAME}:{image.getLatestVersionName()}[/orange3].")
            return  # type: ignore

    def removeImage(self, image: ExegolImage, upgrade_mode: bool = False) -> bool:
        """Remove an ExegolImage from disk"""
        tag = image.removeCheck()
        if tag is None:  # Skip removal if image is not installed locally.
            return False
        with console.status(f"Removing {'previous ' if upgrade_mode else ''}image [green]{image.getName()}[/green]...", spinner_style="blue"):
            try:
                if not image.isVersionSpecific() and image.getInstalledVersionName() != image.getName() and not upgrade_mode:
                    # Docker can't remove multiple images at the same tag, version specific tag must be remove first
                    logger.debug(f"Removing image {image.getFullVersionName()}")
                    if not self.__remove_image(image.getFullVersionName()):
                        logger.critical(f"An error occurred while removing this image : {image.getFullVersionName()}")
                logger.debug(f"Removing image {image.getLocalId()} ({image.getFullVersionName() if upgrade_mode else image.getFullName()})")
                if self.__remove_image(image.getLocalId()):
                    logger.verbose(f"Removing {'previous ' if upgrade_mode else ''}image [green]{image.getName()}[/green]...")
                    logger.success(f"{'Previous d' if upgrade_mode else 'D'}ocker image successfully removed.")
                    return True
            except APIError as err:
                # Handle docker API error code
                logger.verbose(err.explanation)
                if err.status_code == 409:
                    if upgrade_mode:
                        logger.error(f"The '{image.getName()}' image cannot be deleted yet, "
                                     "all containers using this old image must be deleted first.")
                    else:
                        logger.error(f"The '{image.getName()}' image cannot be deleted because "
                                     f"it is currently used by a container. Aborting.")
                elif err.status_code == 404:
                    logger.error(f"This image doesn't exist locally {image.getLocalId()} ({image.getFullName()}). Aborting.")
                else:
                    logger.critical(f"An error occurred while removing this image : {err}")
        return False

    def __remove_image(self, image_name: str) -> bool:
        """
        Handle docker image removal with timeout support
        :param image_name: Name of the docker image to remove
        :return: True is removal successful and False otherwise
        """
        try:
            self.__client.images.remove(image_name, force=False, noprune=False)
            return True
        except ReadTimeout:
            logger.warning("The deletion of the image has timeout. Docker is still processing the removal, please wait.")
            max_retry = 5
            wait_time = 5
            for i in range(5):
                try:
                    _ = self.__client.images.get(image_name)
                    # DockerSDK image getter is an exact matching, no need to add more check
                except APIError as err:
                    if err.status_code == 404:
                        return True
                    else:
                        logger.debug(f"Unexpected error after timeout: {err}")
                except ReadTimeout:
                    wait_time = wait_time + wait_time * i
                    logger.info(f"Docker timeout again ({i + 1}/{max_retry}). Next retry in {wait_time} seconds...")
                    sleep(wait_time)  # Wait x seconds before retry
            logger.error(f"The deletion of the image '{image_name}' has timeout, the deletion may be incomplete.")
        return False

    def buildImage(self, tag: str, build_profile: Optional[str] = None, build_dockerfile: Optional[str] = None, dockerfile_path: str = ConstantConfig.build_context_path):
        """Build a docker image from source"""
        if ParametersManager().offline_mode:
            logger.critical("It's not possible to build a docker image in offline mode. The build process need access to internet ...")
            return False
        logger.info(f"Building exegol image : {tag}")
        if build_profile is None or build_dockerfile is None:
            build_profile = "full"
            build_dockerfile = "Dockerfile"
        logger.info("Starting build. Please wait, this will be long.")
        logger.verbose(f"Creating build context from [gold]{dockerfile_path}[/gold] with "
                       f"[green][b]{build_profile}[/b][/green] profile ({ParametersManager().arch}).")
        if EnvInfo.arch != ParametersManager().arch:
            logger.warning("Building an image for a different host architecture can cause unexpected problems and slowdowns!")
        try:
            # path is the directory full path where Dockerfile is.
            # tag is the name of the final build
            # dockerfile is the Dockerfile filename
            ExegolTUI.buildDockerImage(
                self.__client.api.build(path=dockerfile_path,
                                        dockerfile=build_dockerfile,
                                        tag=f"{ConstantConfig.IMAGE_NAME}:{tag}",
                                        buildargs={"TAG": f"{build_profile}",
                                                   "VERSION": "local",
                                                   "BUILD_DATE": datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')},
                                        platform="linux/" + ParametersManager().arch,
                                        rm=True,
                                        forcerm=True,
                                        pull=True,
                                        decode=True))
            logger.success(f"Exegol image successfully built")
        except APIError as err:
            logger.debug(f"Error: {err}")
            if err.status_code == 500:
                logger.error(f"Error: {err.explanation}")
                logger.error("Error while contacting docker hub. You probably don't have internet. Aborting.")
                logger.debug(f"Error: {err}")
            else:
                logger.critical(f"An error occurred while building this image : {err}")
        except ReadTimeout:
            logger.critical("Received a timeout error, Docker is busy... Unable to build the local image, retry later.")
            return  # type: ignore
