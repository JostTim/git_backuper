import os, sys, io, logging, tomllib, subprocess, requests
from urllib.parse import urlparse
from enum import Enum
from pathlib import Path
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import overload, Literal, Generator, Callable, Optional

RESETED_BRANCHES = []
FAILED_REPOS = []
FAILED_MAPPINGS = []
STDOUT = sys.__stdout__
STDERR = sys.__stderr__
PRINT_BUFFER = io.StringIO()

MAX_WORKERS = 4
EXCLUDE_REPOS = {}
LANGUAGE_MAPPING = {}


def process_config(config: dict) -> str:
    global LANGUAGE_MAPPING, EXCLUDE_REPOS, MAX_WORKERS
    LANGUAGE_MAPPING = dict(config.get("language_mapping", {}))
    EXCLUDE_REPOS = set(config.get("exclude_repositories", []))
    MAX_WORKERS = config.get("max_concurrent_workers", 4)

    return config["backup_path"]


class color_code(Enum):
    magenta = "\033[95m"
    blue = "\033[94m"
    cyan = "\033[96m"
    green = "\033[92m"
    red = "\033[91m"
    bold_dark_red = "\033[1;31m"
    orange = "\033[38;5;214m"
    bold_dark_orange = "\033[1;38;5;208m"
    yellow = "\033[93m"
    white = "\033[97m"
    reset = "\033[0m"


class ColorMeta(type):
    def __getattr__(cls, color_name) -> Callable[[str], str]:
        """Return the color code based on the color name.

        Args:
            cls: The Color class.
            color_name: The name of the color.

        Returns:
            Color: The color code corresponding to the color name.

        Raises:
            AttributeError: If the color name is not found in the color_code members.
        """
        if color_name in color_code.__members__:
            if stdout_supports_color():
                return cls(color_code[color_name])
            return cls(None)  # not format will be applied with a Color instanciated with None
        raise AttributeError(f"type object 'Color' has no attribute '{color_name}'")


class C(metaclass=ColorMeta):
    def __init__(self, color_code: Optional[color_code] = None):
        """Initializes the object with a color code.

        Args:
            color_code (str): The color code to be assigned to the object. Defaults to None.
        """
        self.color_code = color_code

    def __call__(self, message: str) -> str:
        """Apply color code to the given message if color code is set.

        Args:
            message (str): The message to apply color code to.

        Returns:
            str: The message with color code applied, or the original message if color code is not set.
        """
        message = message.strip("\n")
        if self.color_code is None:
            return message
        return f"{self.color_code.value}{message}{color_code.reset.value}"


def stdout_supports_color() -> bool:
    """Check if the standard output supports color.

    Returns:
        bool: True if the standard output supports color, False otherwise.
    """

    term = os.getenv("TERM", "")
    if term in ("xterm", "xterm-color", "xterm-256color", "screen", "screen-256color", "linux", "cygwin"):
        return True

    if os.name == "nt":  # Windows
        return True

    if "COLORTERM" in os.environ:
        return True

    if not sys.stdout.isatty():
        return False

    return False


def log_errors(logger_function: Callable, errors: list[dict], header_message: str):
    level = logger_function.__name__
    if level == "warning":
        color = C.orange
        head_color = C.bold_dark_orange
        emoji = "⚠️  "
    elif level == "error":
        color = C.red
        head_color = C.bold_dark_red
        emoji = "❌  "
    else:
        color = C.blue
        head_color = C.blue
        emoji = "◻️  "

    if errors:
        print(head_color(" " * (37 + len(emoji)) + "_" * len(header_message)))
        logger_function(emoji + head_color(header_message))
    for error in errors:
        logger_function(color(f"\t• {error['message']}"))


def pass_trough(message: str, action: Callable):
    global STDERR, STDOUT

    temp_stdout, temp_stderr = sys.stdout, sys.stderr
    temp_STDOUT, temp_STDERR = STDOUT, STDERR

    sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
    STDOUT, STDERR = sys.__stdout__, sys.__stderr__

    action(message)

    sys.stdout, sys.stderr = temp_stdout, temp_stderr
    STDOUT, STDERR = temp_STDOUT, temp_STDERR


def shunt_stdout():
    global STDOUT, STDERR
    STDOUT = subprocess.DEVNULL
    STDERR = subprocess.DEVNULL
    sys.stdout = PRINT_BUFFER
    sys.stderr = PRINT_BUFFER


def restore_stdout():
    global STDOUT, STDERR
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__
    STDOUT = sys.__stdout__
    STDERR = sys.__stderr__


class EmptyRemoteBranch(Exception):
    pass


class GitLocalRepo(dict, ABC):

    local_path: Path
    name: str
    archived: bool
    clone_url: str
    messages_mapping = {"repo": "From repository : ", "branch": ", with ranch : ", "issue": ", with issue : "}

    def __init__(self, *args, local_root: str | Path, **kwargs):
        """Initialize the object with the given arguments.

        Args:
            *args: Variable length argument list.
            local_root (str): The local root path.
            **kwargs: Arbitrary keyword arguments.

        Returns:
            None
        """
        super().__init__(*args, **kwargs)
        self.initiate()
        self.local_path = self.get_local_path(local_root)

    @abstractmethod
    def initiate(self) -> None:
        """Must initialize the Repo's atrtibutes name, archived and clone_url"""
        ...

    @abstractmethod
    def get_language(self) -> str:
        """Must return the programming language (main) of the Repo as a string."""
        ...

    def get_local_path(self, local_root: str | Path) -> Path:
        """Return the local path for the file based on the provided local root directory.

        Args:
            local_root (str): The root directory where the file will be stored locally.

        Returns:
            Path: The local path for the file.
        """
        if self.archived:
            return Path(local_root) / "archives" / self.name
        return Path(local_root) / self.get_language() / self.name

    def get_logging_info(self, **kwargs) -> dict:
        mapping = self._get_logging_mapping(**kwargs)
        mapping["message"] = self._get_logging_message(mapping)
        return mapping

    def _get_logging_mapping(self, **kwargs) -> dict:
        mapping = {"repo": self.name}
        if (issue := kwargs.get("issue", None)) is not None:
            mapping.update(issue=issue)
        if (branch_name := kwargs.get("branch_name", None)) is not None:
            mapping.update(branch=branch_name)
        return mapping

    def _get_logging_message(self, mapping: dict) -> str:
        message = []

        for key, prefix in self.messages_mapping.items():
            if key in mapping.keys():
                message.append(f"{prefix}{mapping[key]}")

        message = " ".join(message)
        return message

    @property
    def git_loc_prefix(self):
        """Returns a list containing the Git prefix for the current local path.

        Returns:
            list: A list containing the Git prefix for the current local path.
        """
        return ["-C", str(self.local_path)]

    @overload
    def git(self, *args, target=True, out: Literal[False] = False, env=None) -> bool: ...

    @overload
    def git(self, *args, target=True, out: Literal[True] = True, env=None) -> list[str]: ...

    def git(self, *args, target=True, out=False, env=None) -> list[str] | bool:
        """Run a git command with optional arguments.

        Args:
            *args: Additional arguments to pass to the git command.
            target (bool): Whether to include the target prefix in the command (default is True).
            out (bool): Whether to return the output of the command as a list of lines (default is False).

        Returns:
            list: List of output lines if out is True, otherwise a list containing the subprocess run result.
        """

        command: list[str] = ["git", *self.git_loc_prefix, *args] if target else ["git", *args]
        if out:
            return subprocess.check_output(command, env=env).decode().splitlines()
        return bool(subprocess.run(command, env=env, stdout=STDOUT, stderr=STDERR).returncode == 0)

    def fetch_all(self):
        """Fetch all branches and tags from the remote repository."""
        self.git("fetch", "--all")

    def set_active_branch(self, local_branch_name: str):
        """Set the active branch to the specified local branch name.

        Args:
            local_branch_name (str): The name of the local branch to set as active.

        Returns:
            None
        """
        self.git("checkout", local_branch_name)

    def reset_active_from(self, remote_branch_name: str):
        """Reset the active branch to match the specified remote branch.

        Args:
            remote_branch_name (str): The name of the remote branch to reset to.

        Returns:
            None
        """
        RESETED_BRANCHES.append(self.get_logging_info(branch=remote_branch_name.split("/", 1)[1]))
        self.git("reset", "--hard", remote_branch_name)

    def pull_active_from(self, local_branch_name: str, remote_origin_name: str):
        """Pulls the active branch from a specified remote repository and remote branch name.

        Args:
            local_branch_name (str): The name of the local branch to pull changes into.
            remote_origin_name (str): The name of the remote repository to pull changes from.
        """
        self.git("pull", remote_origin_name, local_branch_name)

    def pull_active(self, no_merge_attempt: bool = True):
        """Pull the latest changes from the remote repository.

        Args:
            no_merge_attempt (bool, optional): If True, disable automatic merge commit message editing.
                Defaults to True.
        """
        if no_merge_attempt:
            env = os.environ.copy()
            env["GIT_MERGE_AUTOEDIT"] = "false"
            self.git("pull", "--no-ff", "--no-rebase", env=env)
        else:
            self.git("pull")

    def pull_and_reset_branch_on_fail(self, remote_branch_name: str):
        """Pull the active branch and reset it to the specified remote branch if the pull fails.

        Args:
            remote_branch_name (str): The name of the remote branch to reset to.

        Returns:
            None
        """

        failed = False
        try:
            failed = self.pull_active()
        except subprocess.CalledProcessError as e:
            print(e)
            failed = True

        if failed:
            self.reset_active_from(remote_branch_name)

    def get_status_active(self):
        status: list[str] = self.git("status", out=True)

        status_maps = {
            "synced": ["branch is up to date"],
            "ahead": ["branch is ahead"],
            "behind": ["branch is behind"],
            "untracked": ["untracked files"],
            "unstaged": ["changes not staged for commit"],
            "uncommited": ["changes staged for commit", "changes to be committed"],
            "merge_conflicts": ["unmerged", "merge conflict"],
        }

        statuses = set()
        for line in status:
            if "\t" in line:
                continue
            line = line.lower()
            statuses.update(
                [stat for stat, conditions in status_maps.items() if any([cond in line for cond in conditions])]
            )
        return statuses

    def get_local_branches(self) -> list:
        """Get a list of local branches.

        Returns:
            list: A list of local branches.
        """
        local_branches = self.git("branch", "--list", out=True)
        return [branch.lstrip("* ").strip() for branch in local_branches]

    def local_branch_exists(self, local_branch_name: str) -> bool:
        """Check if a local branch exists.

        Args:
            local_branch_name (str): The name of the local branch to check.

        Returns:
            bool: True if the local branch exists, False otherwise.
        """
        return bool(self.git("branch", "--list", str(local_branch_name), out=True))

    def get_remote_branches(self):
        """Get the names of all remote branches.

        Returns:
            tuple: A tuple containing three elements:
                - A list of local branch names
                - A list of remote branch names
                - A list of remote names
        """
        remote_branches = self.git("branch", "-r", out=True)

        # Get names of all remote branches
        remote_branches = [
            branch.lstrip("* ").strip() for branch in remote_branches if len(branch) and "HEAD" not in branch
        ]

        # remove remote_name/ from branches to get the local name
        # get remote_names of each remote branch if necessary
        try:
            remote_names, local_branches = zip(*[branch.split("/", 1) for branch in remote_branches])
        except Exception as e:
            raise EmptyRemoteBranch(f"Got error {e} when remote_branches value was : {remote_branches}")

        return local_branches, remote_branches, remote_names

    def clone_remote_branch(self, local_branch_name: str, remote_branch_name: str):
        """Clone a remote branch to a new local branch. A local branch with that name mustn't exist already.

        Args:
            local_branch_name (str): The name of the local branch to create.
            remote_branch_name (str): The name of the remote branch to clone.

        Returns:
            None
        """
        self.git("checkout", "-b", local_branch_name, remote_branch_name)

    def sync_all_branches(self):
        """Sync all branches by updating local branches with remote changes.

        This method updates all local branches by pulling updates from their respective remote branches.
        If a local branch does not exist, it creates a new branch from the remote origin branch and switches
        to it as the active branch.

        Args:
            self: The object instance.

        Returns:
            None
        """
        print(f"Updating {self.name} into {self.local_path}...")

        self.fetch_all()

        # Pull updates for each local branch
        for local_branch, remote_branch, origin_name in zip(*self.get_remote_branches()):

            if self.local_branch_exists(local_branch):
                # switches to local_branch as active branch
                self.set_active_branch(local_branch)
            else:
                # creates a new branch with the specified local_branch name
                # from the remote origin branch and immediately switches to that branch as active branch
                self.clone_remote_branch(local_branch, remote_branch)

            status = self.get_status_active()

            if status == {"synced"}:
                # if the branch is up to date, we skip to gain time and avoid flooding the logs
                continue
            elif {"ahead", "untracked", "unstaged", "uncommited", "merge_conflicts"}.intersection(status):
                # as the goal of this code is to make a backup but not a repo to be worked on,
                # if the branch is not only behind but has some unwanted changes, we hard reset it
                self.reset_active_from(remote_branch)
            elif status == {"behind"}:
                # if the branch is just behind, then we try to pull and
                self.pull_and_reset_branch_on_fail(remote_branch)
            else:
                print(
                    self.get_logging_info(
                        branch=local_branch, issue=f"Something strage happened, status of branch is {status}"
                    )["message"]
                )
                self.reset_active_from(remote_branch)

    def clone(self):
        """Clones the repository into the local path.

        This function clones the repository specified by the clone_url attribute into the local_path attribute.
        If the folder specified by local_path does not exist, it will be created.
        """
        print(f"Cloning {self.name} into {self.local_path}...")
        # Creating folder if it doesn't exist
        self.local_path.mkdir(parents=True, exist_ok=True)

        # Cloning the repo into it
        self.git("clone", self.clone_url, str(self.local_path), target=False)

    def sync(self):
        if not self.local_path.exists():
            self.clone()
        try:
            self.sync_all_branches()
        except EmptyRemoteBranch as e:
            FAILED_REPOS.append(self.get_logging_info(issue=f"EmptyRemoteBranch : {e}"))
        except Exception as e:
            FAILED_REPOS.append(self.get_logging_info(issue=f"UnknownError : {e}"))

    @staticmethod
    def load_and_sync(repo_cls: type["GitPlatformRepo"], repository: dict, organization_name: str, local_root):
        # if "github" in str(repository.get("html_url", "")).lower():
        #     platform_class = GithubRepo

        # elif "gitlab" in str(repository.get("web_url", "")).lower():
        #     platform_class = GitlabRepo

        # else:
        #     raise NotImplementedError

        repo = repo_cls(
            repository,
            local_root=Path(local_root) / organization_name,
            language_mapping=LANGUAGE_MAPPING,
            organisation=organization_name,
        )
        repo.sync()
        return {"repo": repo.name, "org": organization_name, "api_url": repo.server}


class GitPlatformRepo(GitLocalRepo):

    topics: list
    language: str
    organisation: str
    messages_mapping = dict(**GitLocalRepo.messages_mapping, organisation=", from Organization : ")
    server: str

    def __init__(self, *args, language_mapping: dict, organisation="", **kwargs):
        """Initialize the class with language mapping.

        Args:
            *args: Variable length argument list.
            language_mapping (dict): A dictionary containing language mapping.
            **kwargs: Arbitrary keyword arguments.
            kwargs must contain local_root with is needed by GitRepo class's __init__
        """
        self.language_mapping = language_mapping
        self.organisation = organisation
        super().__init__(*args, **kwargs)
        self.server = urlparse(self.clone_url).netloc

    def _get_logging_mapping(self, **kwargs):

        mapping = super()._get_logging_mapping(**kwargs)
        if hasattr(self, "organisation") and self.organisation:
            mapping.update(org=self.organisation)
        return mapping

    def get_language(self) -> str:
        """Returns the language based on the mapping of topics to languages.

        Returns:
            str: The language determined based on the mapping of topics to languages.
        """

        def scans_through_mapping(iterable: list[str]):
            return next(
                (
                    lang
                    for lang, associated in self.language_mapping.items()
                    if any((bool(element.lower() in associated) for element in iterable))
                ),
                None,
            )

        # try to get a language from topics
        language = scans_through_mapping(self.topics)
        # if none (beauce user didn't provide the info) fall back to language metric of github (autocalculated)
        if language is None and self.language is not None:
            language = scans_through_mapping([self.language])
            if language is None:
                FAILED_MAPPINGS.append(
                    self.get_logging_info(
                        issue=f"current Topics : {self.topics} and current Main Language : '{self.language}' "
                    )
                )

        return language if language is not None else "others"


class GithubRepo(GitPlatformRepo):

    def initiate(self):
        """Initializes the object with the provided attributes."""
        self.name = self["name"]
        self.archived = self["archived"]
        self.clone_url = self["clone_url"]
        self.topics = self["topics"]
        self.language = self["language"]


class GitlabRepo(GitPlatformRepo):

    def initiate(self):
        """Initializes the object with the provided attributes."""
        self.name = str(self["name"]).replace(" ", "")
        self.archived = self["archived"]
        self.clone_url = self["http_url_to_repo"]
        self.topics = self["topics"]
        self.language = self["language"]


class PlatformApi:

    username: str
    token: str
    root_url: str

    @abstractmethod
    def get_all_repos_mapping_from_executors(
        self, executor_pool: ThreadPoolExecutor
    ) -> Generator[tuple[type[GitPlatformRepo], str, dict], None, None]: ...

    @staticmethod
    def from_config(config: dict) -> "PlatformApi":
        config = config.copy()
        cls: type[PlatformApi] = eval(config.pop("api_class"))
        return cls(**config)


class GitHubApi(PlatformApi):

    repo_class = GithubRepo
    root_url = "https://api.github.com"

    def __init__(self, username, token, visibility="all"):
        """Initializes the class with the provided username and token.

        Args:
            username (str): The username to be assigned.
            token (str): The token to be assigned.
        """

        self.username = username
        self.token = token
        self.visibility = visibility

    def fetch(self, endpoint) -> list[dict]:
        """Fetch data from the specified endpoint.

        Args:
            endpoint (str): The endpoint to fetch data from.

        Returns:
            list: A list of elements fetched from the endpoint.
        """
        elements = []
        page = 1
        while True:
            url = f"{self.root_url}/{endpoint}?visibility={self.visibility}&per_page=100&page={page}"
            response = requests.get(url, auth=(self.username, self.token))
            if response.status_code != 200:
                break
            data = response.json()
            if not data:
                break
            elements.extend(data)
            page += 1
        return elements

    def get_user_orgs(self) -> list[dict]:
        """Fetch all organizations the authenticated user is a member of."""
        print(f"Getting user organizations for {self.username}")
        return self.fetch(endpoint="user/orgs")

    def get_user_repositories(self) -> list[dict]:
        """Fetches repositories of the user.

        Returns:
            List: A list of repositories belonging to the user.
        """
        logging.info(
            f'🌐  {C.blue(f"Getting user projects for")} {C.yellow(self.username)} '
            f'{C.blue("from")} {C.cyan(self.root_url)}'
        )
        return self.fetch(endpoint=f"users/{self.username}/repos")

    def get_org_repositories(self, organization) -> list[dict]:
        """Get repositories of a specific organization.

        Args:
            organization: A string representing the name of the organization or
                a dictionary containing a 'login' key with the organization name.

        Returns:
            A list of repositories belonging to the specified organization.

        Raises:
            TypeError: If the organization is not a string or a dictionary with a 'login' key.
        """
        organization_name = self.get_organization_name(organization)
        print(f"Getting organisation repositories for {organization_name}")
        return self.fetch(endpoint=f"orgs/{organization_name}/repos")

    def get_organization_name(self, organization: dict | str) -> str:
        """Get the name of the organization.

        Args:
            organization (str or dict): The organization name or a dictionary containing a 'login' key that
                is the organization name.

        Returns:
            str: The name of the organization.

        Raises:
            TypeError: If organization is not a string or a dictionary containing a 'login' key.
        """
        if isinstance(organization, dict):
            organization_name = organization["login"]
        elif isinstance(organization, str):
            organization_name = organization
        else:
            raise TypeError(
                "organization must be string or dict containing a 'login' key that is the organisation name"
            )
        return organization_name

    def get_repos_mapping(self, organization) -> list[tuple[str, dict]]:
        """Get the mapping of repositories for a given organization.

        Args:
            organization (str): The name of the organization.

        Returns:
            list[tuple[str, dict]]: A list of tuples containing the organization name and repository information.
        """
        organization_name = self.get_organization_name(organization)
        if organization_name == "Perso":
            mapping = [("Perso", repo) for repo in self.get_user_repositories()]
        elif organization_name == self.username:
            mapping = [(self.username, repo) for repo in self.get_user_repositories()]
        else:
            mapping = [(organization_name, repo) for repo in self.get_org_repositories(organization_name)]
        return self.filter_repos(mapping)

    def filter_repos(self, mapping: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
        """Filters out repositories based on exclusion list.

        Args:
            mapping (list[tuple[str, dict]]): A list of tuples where each tuple contains an organization name
                and a dictionary representing a repository.

        Returns:
            list[tuple[str, dict]]: A filtered list of tuples containing organization names and repository dictionaries.
        """

        def is_repo_included(element):
            org_name, repo = element
            combined_name = f"{org_name}/{repo['name']}"
            if combined_name in EXCLUDE_REPOS:
                return False
            return True

        return list(filter(is_repo_included, mapping))

    def get_all_repos_mapping_from_executors(
        self, executor_pool: ThreadPoolExecutor
    ) -> Generator[tuple[type[GitPlatformRepo], str, dict], None, None]:
        """Reading the repos and organizations repos available for the user, over http, on multiple threads.

        Args:
            executor_pool (ThreadPoolExecutor): The ThreadPoolExecutor instance to execute the tasks.

        Returns:
            list: A list of all repositories mapping from the executors.
        """

        # reading the repos and organizations repos available for the user, over http, on multiple threads
        user_orgs_job = executor_pool.submit(self.get_user_orgs)
        repo_job = [
            executor_pool.submit(self.get_repos_mapping, organisation) for organisation in user_orgs_job.result()
        ] + [executor_pool.submit(self.get_repos_mapping, self.username)]

        return ((self.repo_class, org, repo) for job in as_completed(repo_job) for org, repo in job.result())


class GitLabApi(PlatformApi):
    """
    Because GitLab wants to be different from everyone else, repositories are named projects,
    and organizations are named groups. Groups can, be nested (contrary to github).
    For sake of unification, the names of the methods of this class follows the logic of GitHub naming convention.
    """

    repo_class = GitlabRepo

    def __init__(self, username, token, server="gitlab.com", api_version=4):
        """Initializes the class with the provided username and token.

        Args:
            username (str): The username to be assigned.
            token (str): The token to be assigned.
        """
        self.username = username
        self.token = token
        self.root_url = f"https://{server}/api/v{api_version}"

    def fetch(self, endpoint, *args, no_pages=False):
        """Fetch data from the specified endpoint.

        Args:
            endpoint (str): The endpoint to fetch data from.

        Returns:
            list: A list of elements fetched from the endpoint.
        """
        elements = []
        page = 1
        while True:
            if no_pages:
                arguments = "&".join(list(args))
            else:
                arguments = "&".join(list(args) + ["per_page=100", f"page={page}"])
            url = f"{self.root_url}/{endpoint}?{arguments}"
            headers = {"PRIVATE-TOKEN": self.token}
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                print(f"Error: {response.status_code} - {response.text}")
                break
            data = response.json()
            if no_pages:
                elements = data
                break
            if not data:
                if no_pages:
                    continue
                else:
                    break
            else:
                if no_pages:
                    elements = data
                    break
                else:
                    elements.extend(data)
            page += 1
        return elements

    def get_user_orgs(self):
        """Fetch all groups the authenticated user is a member of."""
        print(f"Getting user groups for {self.username}")
        return self.fetch(endpoint="groups")

    def get_user_repositories(self):
        """Fetches projects of the user.

        Returns:
            List: A list of projects belonging to the user.
        """
        logging.info(
            f'🌐  {C.blue(f"Getting user projects for")} {C.yellow(self.username)} '
            f'{C.blue("from")} {C.cyan(self.root_url)}'
        )
        return self.fetch("projects", "owned=true")

    # def get_group_projects(self, group_id):
    #     """Get projects of a specific group.

    #     Args:
    #         group_id: The ID or name of the group.

    #     Returns:
    #         A list of projects belonging to the specified group.
    #     """
    #     print(f"Getting group projects for {group_id}")
    #     return self.fetch(endpoint=f"groups/{group_id}/projects")

    def get_organization_name(self, repository: dict | str) -> str:
        if isinstance(repository, dict):
            organization_name = str(repository["name_with_namespace"]).replace(" ", "").rsplit("/", 1)[0]
        elif isinstance(repository, str):
            organization_name = repository
        else:
            raise TypeError(
                "organization must be string or dict containing a 'full_name' key that is the organisation name"
            )
        return organization_name

    def get_languages(self, repository_id: str) -> dict:
        languages: dict = self.fetch(f"projects/{repository_id}/languages", no_pages=True)  # type: ignore
        if len(languages):
            language = list(languages.keys())[list(languages.values()).index(max(languages.values()))]
        else:
            language = ""
        # repository.update(languages=languages, language=language)
        # print(f"Got languages for {repository['name_with_namespace']}")
        return {"languages": languages, "language": language}

    def get_repos_mapping(self, organization: str = "") -> list[tuple[str, dict]]:
        """Get the mapping of projects for a given group.

        Args:
            group (str): The name or ID of the group.

        Returns:
            list[tuple[str, dict]]: A list of tuples containing the group name and project information.
        """
        return self.filter_repos(
            [(self.get_organization_name(repository), repository) for repository in self.get_user_repositories()]
        )

    def filter_repos(self, mapping: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
        """Filters out projects based on exclusion list.

        Args:
            mapping (list[tuple[str, dict]]): A list of tuples where each tuple contains a group name
                and a dictionary representing a project.

        Returns:
            list[tuple[str, dict]]: A filtered list of tuples containing group names and project dictionaries.
        """

        def is_repo_included(element):
            organization_name, repository = element
            combined_name = repository["path_with_namespace"].replace(" ", "").lower()
            if combined_name in EXCLUDE_REPOS:
                return False
            return True

        return list(filter(is_repo_included, mapping))

    def get_all_repos_mapping_from_executors(
        self, executor_pool: ThreadPoolExecutor
    ) -> Generator[tuple[type[GitPlatformRepo], str, dict], None, None]:

        def is_repo_included(repository):
            combined_name = repository["path_with_namespace"].replace(" ", "")
            if combined_name in EXCLUDE_REPOS:
                return False
            return True

        repos_mapping = self.get_user_repositories()
        repos_lang_jobs = {executor_pool.submit(self.get_languages, repo["id"]): repo for repo in repos_mapping}

        return (
            (
                self.repo_class,
                self.get_organization_name(repos_lang_jobs[job]),
                dict(**repos_lang_jobs[job], **job.result()),
            )
            for job in as_completed(repos_lang_jobs)
            if is_repo_included(repos_lang_jobs[job])
        )


def run(level="INFO"):

    with open(Path(__file__).parent.parent.parent / ".secret_config.toml", "rb") as f:
        config = tomllib.load(f)

    backup_path = process_config(config)

    level = level.upper()
    logging.basicConfig(level=level, format="%(asctime)s - %(levelname)-8s # %(message)s")
    logger = logging.getLogger()

    platforms = [PlatformApi.from_config(conf) for conf in config.get("platforms", [])]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor_pool:

        if not level == "DEBUG":
            shunt_stdout()

        generators = [platform.get_all_repos_mapping_from_executors(executor_pool) for platform in platforms]
        repositories = [item for generator in generators for item in generator]

        restore_stdout()

        logger.info(
            f'🔎  {C.blue("Found")} {C.green(str(len(repositories)))} '
            f'{C.blue("repos to synchronize. Splitting sync tasks between")} '
            f'{C.green(str(MAX_WORKERS))} {C.blue("threads.")}'
        )

        if not level == "DEBUG":
            shunt_stdout()

        # synchronizing the repos with the drive, as they get available
        sync_jobs = [
            executor_pool.submit(GitPlatformRepo.load_and_sync, cls, repository, organisation, backup_path)
            for cls, organisation, repository in repositories
        ]

        for job in as_completed(sync_jobs):
            result = job.result()
            logger.info(
                f'✅  {C.blue("Finished synchronizing")} {C.magenta(result["repo"])} {C.blue("in")} '
                f'{C.yellow(result["org"])} {C.blue("from")} {C.cyan(result["api_url"])}'
            )

    restore_stdout()

    logger.info(
        f'🎉  {C.blue("Completed synchronization for")} {C.green(str(len(sync_jobs)))} {C.blue("repositories.")}🔐'
    )

    log_errors(logger.error, RESETED_BRANCHES, "Reseted the branches :")
    log_errors(logger.error, FAILED_REPOS, "Failed pulling completely the repos :")
    log_errors(logger.warning, FAILED_MAPPINGS, "Could not infer the language from these mappings :")


if __name__ == "__main__":
    run()
