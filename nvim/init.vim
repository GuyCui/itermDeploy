" =======================
" 通过 vim-plug 安装插件
" =======================
call plug#begin('~/.config/nvim/plugged')
" 美化插件
Plug 'hardcoreplayers/spaceline.vim'
Plug 'hardcoreplayers/vim-buffet'
Plug 'ryanoasis/vim-devicons'
" Plug 'mhinz/vim-startify'
Plug 'hardcoreplayers/dashboard-nvim'
Plug 'Yggdroot/indentLine',{ 'for': ['cpp', 'py', 'json', 'go', 'java', 'vim', 'hs'] }
" 主题插件
Plug 'kristijanhusak/vim-hybrid-material'
Plug 'hardcoreplayers/oceanic-material'
Plug 'mhartington/oceanic-next'
" Debugger
Plug 'puremourning/vimspector', {'do': './install_gadget.py --enable-c --enable-python --enable-go'}

" File navigation
Plug 'kevinhwang91/rnvimr'

" 功能插件
" vim dashboard 启动页表盘
Plug 'glepnir/dashboard-nvim'
Plug 'liuchengxu/vim-clap', { 'do': ':Clap install-binary' }
Plug 'tenfyzhong/CompleteParameter.vim',{'for':['cpp', 'py', 'go', 'java', 'vim', 'json', 'hs']}

" 编辑plist文件
Plug 'darfink/vim-plist'

" support the ranger cli
" F2 wake up
Plug 'francoiscabrol/ranger.vim'
Plug 'rbgrouleff/bclose.vim'
"  在浮动/弹出窗口中使用nvim / vim的内置终端
Plug 'voldikss/vim-floaterm'

Plug 'metalelf0/supertab'
Plug 'mg979/vim-visual-multi', {'branch': 'master'}
Plug 'junegunn/vim-easy-align', {'on':'<Plug>(EasyAlign)'}
Plug 'voldikss/vim-translator', { 'on':'<Plug>Translate' }
Plug 'rhysd/accelerated-jk', {'on':['<Plug>(accelerated_jk_gj)' , '<Plug>(accelerated_jk_gk)']}
Plug 'tyru/open-browser.vim', {'on':['<Plug>(openbrowser-smart-search)', '<Plug>(openbrowser-open)']}
Plug 'easymotion/vim-easymotion'
Plug 'kien/rainbow_parentheses.vim'
Plug 'chrisbra/changesPlugin'

" Taglist强大的tags展示与跳转
Plug 'liuchengxu/vista.vim', {'on':'Vista'}

Plug 'kristijanhusak/defx-git', {'on':'Defx'}
Plug 'Shougo/defx.nvim', { 'do': ':UpdateRemotePlugins'}
" defx 图标插件
Plug 'kristijanhusak/defx-icons'
Plug 'voldikss/vim-floaterm', { 'on': 'FloatermNew' }
Plug 'sbdchd/neoformat', {'on':'Neoformat'}
Plug 'honza/vim-snippets',{ 'for': ['c', 'h', 'cpp', 'py', 'json', 'go', 'java', 'vim', 'hs'] }
Plug 'dhruvasagar/vim-table-mode', { 'for': 'markdown' }
Plug 'suan/vim-instant-markdown', {'for': 'markdown'}
Plug 'fatih/vim-go', { 'do': ':GoUpdateBinaries' }
Plug 'junegunn/fzf', { 'do': { -> fzf#install() } }
Plug 'scrooloose/nerdcommenter'
"左边显示git修改删除增加的标记
Plug 'airblade/vim-gitgutter'

" tmux支持
Plug 'christoomey/vim-tmux-navigator'
Plug 'edkolev/tmuxline.vim'

" 其他视觉增强
Plug 'mg979/vim-xtabline'

" markdown
" Autoformat
Plug 'Chiel92/vim-autoformat'

Plug 'vim-scripts/vim-auto-save'

" 高亮TODO
Plug 'sakshamgupta05/vim-todo-highlight'
" coc插件下载
Plug 'neoclide/coc.nvim',{'branch':'release'}
Plug 'jiangmiao/auto-pairs'
Plug 'sheerun/vim-polyglot',{'for':['c', 'h', 'cpp', 'py', 'go', 'java', 'vim', 'json', 'hs']}
call plug#end()


"
" ██████╗ ██████╗ ██████╗ ███████╗  ██╗   ██╗██╗███╗   ███╗
"██╔════╝██╔═══██╗██╔══██╗██╔════╝  ██║   ██║██║████╗ ████║
"██║     ██║   ██║██████╔╝█████╗    ██║   ██║██║██╔████╔██║
"██║     ██║   ██║██╔══██╗██╔══╝    ╚██╗ ██╔╝██║██║╚██╔╝██║
"╚██████╗╚██████╔╝██║  ██║███████╗██╗╚████╔╝ ██║██║ ╚═╝ ██║
" ╚═════╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═╝ ╚═══╝  ╚═╝╚═╝     ╚═╝

"============
"首次安装使用
"============
"Vim-Plug的首次下载安装
if empty(glob('~/.config/nvim/autoload/plug.vim'))
	silent !curl -fLo ~/.config/nvim/autoload/plug.vim --create-dirs
				\ https://raw.githubusercontent.com/junegunn/vim-plug/master/plug.vim
	autocmd VimEnter * PlugInstall --sync | source $MYVIMRC
endif


" ===========
" 模块化配置
" ===========
" 基本键位配置
source ~/.config/nvim/core/key_bindings.vim
" 常规配置
source ~/.config/nvim/core/general_config.vim
" 主题配置
source ~/.config/nvim/core/theme.vim
" 程序语言编译配置
source ~/.config/nvim/core/language_config.vim
" 功能配置
source ~/.config/nvim/core/plug_settings.vim
" Python 配置
source ~/.config/nvim/core/special_config.vim
