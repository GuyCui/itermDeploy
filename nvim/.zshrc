# Path to your oh-my-zsh installation.
export ZSH="$HOME/.oh-my-zsh"
export MYVIMRC="~/.config/nvim"
export NEOFETCH="neofetch --iterm2 ~/Library/Mobile\ Documents/com~apple~CloudDocs/阴阳师.jpg"
alias s=$NEOFETCH

# 设置zsh主题
ZSH_THEME="powerlevel10k/powerlevel10k"
POWERLEVEL9K_MODE='nerdfont-complete'

POWERLEVEL9K_LEFT_SEGMENT_SEPARATOR='\uE0C0'

POWERLEVEL9K_RIGHT_SEGMENT_SEPARATOR='\uE0C2'

# 设置左边元素
POWERLEVEL9K_LEFT_PROMPT_ELEMENTS=(os_icon dir dir_writable vcs virtualenv prompt_char)
# 设置右边元素
POWERLEVEL9K_RIGHT_PROMPT_ELEMENTS=(status java_version jenv public_ip background_jobs command_execution_time battery time)
# github目录显示
POWERLEVEL9K_VCS_GIT_GITHUB_ICON=$'\uf296'

POWERLEVEL9K_SHORTEN_DIR_LENGTH=2
POWERLEVEL9K_SHORTEN_DELIMITER=..
POWERLEVEL9K_PROMPT_ON_NEWLINE=true
POWERLEVEL9K_MULTILINE_FIRST_PROMPT_PREFIX=$'\n'
POWERLEVEL9K_MULTILINE_LAST_PROMPT_PREFIX="\uF460%F{073}\uF460%F{109}\uF460%f "

export UPDATE_ZSH_DAYS=13
HIST_STAMPS="yyyy/mm/dd"

plugins=(
		git
		z
		brew
		zsh-history-substring-search
		zsh-autosuggestions
		zsh-syntax-highlighting
)
ZSH_AUTOSUGGEST_HIGHLIGHT_STYLE='fg=240'

alias config="vim ~/.zshrc"
alias ansibleconfig="vim ~/.ansible/ansible.cfg"
alias grep='grep --color=auto'
alias ls='colorls -lA --sd'
alias rg="ranger"

# You may need to manually set your language environment
 export LANG=en_US.UTF-8

# Preferred editor for local and remote sessions
 if [[ -n $SSH_CONNECTION ]]; then
   export EDITOR='nvim'
 else
   export EDITOR='vim'
 fi


# 优先使用 Neovim
if type nvim > /dev/null 2>&1; then
      alias vim='nvim'
fi

[ -f ~/.fzf.zsh ] && source ~/.fzf.zsh

# >>> conda initialize >>>
# !! Contents within this block are managed by 'conda init' !!
__conda_setup="$('$HOME/miniconda3/bin/conda' 'shell.zsh' 'hook' 2> /dev/null)"
if [ $? -eq 0 ]; then
    eval "$__conda_setup"
else
    if [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
        . "$HOME/miniconda3/etc/profile.d/conda.sh"
    else
        export PATH="$HOME/miniconda3/bin:$PATH"
    fi
fi
unset __conda_setup
# <<< conda initialize <<<
export PATH=$HOME/miniconda3/bin:$PATH

# modify the prompt to contain git branch name if applicable
git_prompt_info() {
  ref=$(git symbolic-ref HEAD 2> /dev/null)
  if [[ -n $ref ]]; then
    echo " %{$fg_bold[green]%}${ref#refs/heads/}%{$reset_color%}"
  fi
}
setopt promptsubst
export PS1='${SSH_CONNECTION+"%{$fg_bold[green]%}%n@%m:"}%{$fg_bold[blue]%}%c%{$reset_color%}$(git_prompt_info) %# '

# load our own completion functions
fpath=(~/.zsh/completion $fpath)

# completion
autoload -U compinit
compinit

# makes color constants available
autoload -U colors
colors

# enable colored output from ls, etc
export CLICOLOR=1

# history settings
setopt hist_ignore_all_dups inc_append_history
HISTFILE=~/.zhistory
HISTSIZE=4096
SAVEHIST=4096

# awesome cd movements from zshkit
setopt autocd autopushd pushdminus pushdsilent pushdtohome cdablevars
DIRSTACKSIZE=5

# Enable extended globbing
setopt extendedglob

# Allow [ or ] whereever you want
unsetopt nomatch


# 高亮显示
source /usr/local/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh

# use vim as the visual editor
export VISUAL=vim
export EDITOR=$VISUAL

#  brew 管理记录访问
[[ -s $(brew --prefix)/etc/profile.d/autojump.sh ]] && . $(brew --prefix)/etc/profile.d/autojump.sh


# load rbenv if available
if which rbenv &>/dev/null ; then
  eval "$(rbenv init - --no-rehash)"
fi

# load thoughtbot/dotfiles scripts
export PATH="$HOME/.bin:$PATH"

# mkdir .git/safe in the root of repositories you trust
export PATH=".git/safe/../../bin:$PATH"

source $ZSH/./oh-my-zsh.sh

[[ -f ~/.aliases ]] && source ~/.aliases

# Local config
[[ -f ~/.zshrc.local ]] && source ~/.zshrc.local && source ~/.zshrc
source /usr/local/opt/powerlevel10k/powerlevel10k.zsh-theme

# 添加maven
export MAVEN_HOME=`pwd`/IdeaProjects/apache-maven-3.6.3
export PATH=$PATH:$MAVEN_HOME/bin

# Add RVM to PATH for scripting. Make sure this is the last PATH variable change.
export PATH="$PATH:$HOME/.rvm/bin"

export PATH="`pwd`/bin/Sencha/Cmd:$PATH"
export PATH="/usr/local/opt/gnu-getopt/bin:$PATH"
export PUB_HOSTED_URL=https://pub.flutter-io.cn
export FLUTTER_STORAGE_BASE_URL=https://storage.flutter-io.cn
export PATH="$PATH:`pwd`/flutter/bin"
export PATH=$PATH:/Users/cuizhihui/IdeaProjects/apache-maven-3.6.3/bin

