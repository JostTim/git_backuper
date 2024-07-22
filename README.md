# git_backuper
A python CLI tool to backup locally a whole bunch of git repos from your git platforms accounts, all in one go.

Git Backups !

## How to use :

- clone with :

    `git clone https://github.com/JostTim/git_backuper`

- go to the repo that you pulled with :

    `cd C:/users/MyName/git_backuper`

- install with pdm by running : 

    `pdm install`

    (Note : if not already available for you, you can install pdm with ``pip install pdm``. This git_backuper package can of course also be installed and used with pip if you prefer. But i like to advocate for pdm as it is very nice and automatically makes your projects scoped in their environment to avoid having big piles of anaconda environments)

- **`edit the config to your needs`**

    Enter the url of the git platform you want to dump your repos from in the **config file** (only github and gitlab servers are supported right now, I might add gitbucket API etc.. later), tune it more as you like.  
    (**find detailed infos about the config tuning below**)

- then run :

    `pdm run git-backuper`

## Edit the config :

The ``.secret_config.toml`` file contains sensitive info as your username and token are required to use the API in order to pull all your repos to your local computer, automatically.

Make sure the config file is named ``.secret_config.toml`` and is not included in your git history if you fork this repo ! 

Have a look at the ``.example_config.toml`` and find infos about the fields here :

- ``backup_path``  :
    tune the location of the folder that will recieve the backups, 
    
- ``max_concurrent_workers`` : 
    how many threads to use in parallel (speeds up the process a lot if you have many repos, you should not use more than the available number of cores you have as it is not very efficient at this point)
    
- ``exclude_repositories`` :
    the repos you want to exclude (username/repo_name or organization_name/repo_name in the exclude_repositories field of the config file). 

- `platforms` :    
    You can add as many platforms as you want, they are all fetched in series (in fact in parallel as many threads as you have set).
    A platform conf must have the ``api_class`` field set. For now, only two possibilities : `GitHubApi` or `GitLabApi`.
    If using gitlab, you should supply the `server` field of your hosted gitlab server. If not supplied, the central gitlab.com instance is used. The api is by defaul v4 nowadays on any gitlab instance, but you can tune it if needed.
    Your `username` and `token` are needed here, for each platform you set, to fetch the repos using the API of the given platform.

- `language_mapping` : 
    This allows you to tune how the repose are organized in the `backup_path`/`organization or username` folder.
    They try to follow a language name that you supplied in the topics of the repo or the most used language (in that priority order). The mapping allows you to set `key` : wich subfolder to direct the repo backed up to, `values` in wich conditions matched. (topics are matched against your values, if non matches, the most used language does the same, if still not matched, the subfolder will be : `other`)

## Example config : 

file **`.secret_config.toml`**
located at the root of the repo (same folder where the src and pyproject.toml sits)

```toml
max_concurrent_workers = 16
backup_path = "C:/Users/MyName/Documents/RepositoriesBackups"
exclude_repositories = [
    "MyName/AnOldRepo",
    "MyName/AnArchivedRepoThaIDontWant",
    "MyNameOrganization/AnArchivedRepoThaIDontWant",
    "MyUpperGroup/MysecondGroup/AnOldGitLabRepo",
]

[language_mapping]
web_front = ["javascript", "html", "css"]
python = ["python"]
c-lang = ["cplusplus", "c++", "c", "clang", "c-language"]
matlab = ["matlab"]
documentation = ["doc", "documentation"]
jupyter_notebooks = ["jupyter notebook", "jupyter-notebook", "notebook"]

[[platforms]]
api_class = "GitHubApi"
token = "ghp_1234567890abcdefghijklmnopqrstuvwx"
username = "MyName"
visibility = "all"

[[platforms]]
api_class = "GitLabApi"
server = "gitlab.com"
api_version = 4
token = "1234567890abcdefghijklmnopq"
username = "MyName"
```

## VERY IMPORTANT :
Note that his code is meant to make backups of all your various repositories available online (public as well as private, thanks to the token. Right now i didn't make a public only version that would not need a token to work, but i might in the future), and automate their frequent update to stay on the latest version available of the repos.
This means it WILL make destructive operations, and discard changes if there are merges conflicts or things that prevent it from pulling the branches to the latest versions (it drops and re-pulls branches it cannot pull if behind remote.) As such, the repos that are located in the folders after pulling should NOT be used for developpement, only as backups in case you want your data and workd to be with you !

The codebase is relatively light, no extra library used (except requests, i might try to use http.client and urllib to replace it at some point, to be 100% relying on built in packages only.), so you can easily have a look at the source code if you are worried about the token info being sent somewhere. (which you should be, it's a very dangerous vulnerability to leak around)