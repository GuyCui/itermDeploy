# Enable Powerlevel10k instant prompt. Should stay close to the top of ~/.zshrc.
# Initialization code that may require console input (password prompts, [y/n]
# confirmations, etc.) must go above this block; everything else may go below.
if [[ -r "${XDG_CACHE_HOME:-$HOME/.cache}/p10k-instant-prompt-${(%):-%n}.zsh" ]]; then
  source "${XDG_CACHE_HOME:-$HOME/.cache}/p10k-instant-prompt-${(%):-%n}.zsh"
fi
export USER='mingzhi Cui.dev'
export LANGUAGE=en_US.UTF-8
# If you come from bash you might have to change your $PATH.
# export PATH=$HOME/bin:/usr/local/bin:$PATH

# Path to your oh-my-zsh installation.
export ZSH=$HOME/.oh-my-zsh

# 导入antigen.zsh文件
source ~/.antigen.zsh
# 使用ohmyzsh
antigen use oh-my-zsh

# 添加要使用的ohmyzsh插件，可以在ohmyzsh项目plugins文件夹下找到
antigen bundle command-not-found
antigen bundle zsh-users/zsh-syntax-highlighting
antigen bundle zsh-users/zsh-completions
antigen bundle zsh-users/zsh-autosuggestions
# 设置主题，主题可以在ohmyzsh项目themes下找到
antigen theme robbyrussell

# 最后必须提交确认修改
antigen apply


# Set name of the theme to load --- if set to "random", it will
# load a random theme each time oh-my-zsh is loaded, in which case,
# to know which specific one was loaded, run: echo $RANDOM_THEME
# See https://github.com/ohmyzsh/ohmyzsh/wiki/Themes
# ZSH_THEME="robbyrussell"

# ZSH_THEME="agnoster"
# ZSH_THEME="powerlevel10k/powerlevel10k"
ZSH_THEME="spaceship"

# 显示时间
SPACESHIP_TIME_SHOW="true"

# Set list of themes to pick from when loading at random
# Setting this variable when ZSH_THEME=random will cause zsh to load
# a theme from this variable instead of looking in $ZSH/themes/
# If set to an empty array, this variable will have no effect.
# ZSH_THEME_RANDOM_CANDIDATES=( "robbyrussell" "agnoster" )

# Uncomment the following line to use case-sensitive completion.
# CASE_SENSITIVE="true"

# Uncomment the following line to use hyphen-insensitive completion.
# Case-sensitive completion must be off. _ and - will be interchangeable.
# HYPHEN_INSENSITIVE="true"

# Uncomment the following line to disable bi-weekly auto-update checks.
# DISABLE_AUTO_UPDATE="true"

# Uncomment the following line to automatically update without prompting.
# DISABLE_UPDATE_PROMPT="true"

# Uncomment the following line to change how often to auto-update (in days).
# export UPDATE_ZSH_DAYS=13

# Uncomment the following line if pasting URLs and other text is messed up.
# DISABLE_MAGIC_FUNCTIONS="true"

# Uncomment the following line to disable colors in ls.
# DISABLE_LS_COLORS="true"

# Uncomment the following line to disable auto-setting terminal title.
# DISABLE_AUTO_TITLE="true"

# Uncomment the following line to enable command auto-correction.
# ENABLE_CORRECTION="true"

# Uncomment the following line to display red dots whilst waiting for completion.
# Caution: this setting can cause issues with multiline prompts (zsh 5.7.1 and newer seem to work)
# See https://github.com/ohmyzsh/ohmyzsh/issues/5765
# COMPLETION_WAITING_DOTS="true"

# Uncomment the following line if you want to disable marking untracked files
# under VCS as dirty. This makes repository status check for large repositories
# much, much faster.
# DISABLE_UNTRACKED_FILES_DIRTY="true"

# Uncomment the following line if you want to change the command execution time
# stamp shown in the history command output.
# You can set one of the optional three formats:
# "mm/dd/yyyy"|"dd.mm.yyyy"|"yyyy-mm-dd"
# or set a custom format using the strftime function format specifications,
# see 'man strftime' for details.
# HIST_STAMPS="mm/dd/yyyy"
HIST_STAMPS="yyyy-mm-dd"

# Would you like to use another custom folder than $ZSH/custom?
# ZSH_CUSTOM=/path/to/new-custom-folder

# Which plugins would you like to load?
# Standard plugins can be found in $ZSH/plugins/
# Custom plugins may be added to $ZSH_CUSTOM/plugins/
# Example format: plugins=(rails git textmate ruby lighthouse)
# Add wisely, as too many plugins slow down shell startup.
# plugins=(git)
plugins=(
  you-should-use
  git
  hitokoto
  command-not-found
  vi-mode
  cp
  zsh-completions
  sudo
  autojump
  git-open
  autojump
  zsh-autosuggestions
  zsh-syntax-highlighting
)
autoload -U compinit && compinit
source $ZSH/oh-my-zsh.sh

# 打开配置文件
alias config="code ~/.zshrc"
# 重新加载配置文件
alias reload="source ~/.zshrc"

# npm 清理
alias npmclean="rm -rf node_modules && rm -rf package-lock.json"
# npm 重装
alias npmreinstall="rm -rf node_modules && rm -rf package-lock.json && npm install"

# git 设置别名
alias g="git"

alias typora="open -a typora"

function copy() {
	pbcopy < $1
}

alias now='date "+%Y-%m-%d %H:%M:%S"'

alias -s {js,css,java,json}='code'      # 在命令行直接输入 python 文件，会用 vim 中打开，以下类似
alias -s md='code'
alias -s txt='vim'

alias mkdir='mkdir -pv'

# 查看文件/目录大小

alias size='f(){ du -sh $1* | sort -hr; }; f'

alias cp='cp -i'
alias rm='rm -i'
alias mv='mv -i'

alias cls='clear'

alias gs='git status'
alias gc='git checkout'
alias log="git log --pretty=format:'%Cred%h%Creset -%C(yellow)%d%Creset %s %Cgreen(%cr) %C(bold blue)<%an>%Creset' --abbrev-commit --date=relative"

alias lock="pmset displaysleepnow"

alias kill="pkill"

# alias npm="sudo npm"
# alias yarn="sudo yarn"

alias weather='curl wttr.in'


# User configuration

# export MANPATH="/usr/local/man:$MANPATH"

# You may need to manually set your language environment
# export LANG=en_US.UTF-8

# Preferred editor for local and remote sessions
# if [[ -n $SSH_CONNECTION ]]; then
#   export EDITOR='vim'
# else
#   export EDITOR='mvim'
# fi
export NODE_OPTIONS=--max_old_space_size=4096
export EDITOR='code'

# Compilation flags
# export ARCHFLAGS="-arch x86_64"

# Set personal aliases, overriding those provided by oh-my-zsh libs,
# plugins, and themes. Aliases can be placed here, though oh-my-zsh
# users are encouraged to define aliases within the ZSH_CUSTOM folder.
# For a full list of active aliases, run `alias`.
#
# Example aliases
# alias zshconfig="mate ~/.zshrc"
# alias ohmyzsh="mate ~/.oh-my-zsh"
# 设置homebrew路径
# export PATH=$PATH:/opt/homebrew/bin;
path=('/opt/homebrew/bin' $path)
export PATH

# To customize prompt, run `p10k configure` or edit ~/.p10k.zsh.
[[ ! -f ~/.p10k.zsh ]] || source ~/.p10k.zsh

# 设置目录快速跳转插件
[ -f /opt/homebrew/etc/profile.d/autojump.sh ] && . /opt/homebrew/etc/profile.d/autojump.sh
