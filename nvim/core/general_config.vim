set mouse=nv
set cursorline
set report=0
set errorbells
set visualbell
set hidden
set autochdir
" 显示光标位置
set ruler
" 左下角显示当前 vim 模式
set showmode
set fileformats=unix,dos,mac
set magic
set path+=**
set isfname-==
set virtualedit=block
set synmaxcol=2500
set formatoptions+=1
set formatoptions-=t

set formatoptions-=o
" 自动保存
set autowrite
" 突出显示当前列
set cursorcolumn
"设置光标样式为竖线vertical bar
" Change cursor shape between insert and normal mode in iTerm2.app
"if $TERM_PROGRAM =~ "iTerm"
let &t_SI = "\<Esc>]50;CursorShape=1\x7" " Vertical bar in insert mode
let &t_EI = "\<Esc>]50;CursorShape=0\x7" " Block in normal mode
"endif
" 共享剪贴板
set clipboard+=unnamed
" 文件被改动时自动载入
set autoread
" 顶部底部保持3行距离
set scrolloff=3

let g:plist_display_format = 'xml'
let g:plist_save_format = ''

if has('patch-7.3.541')
	set formatoptions+=j
endif

if has('vim_starting')
	set encoding=utf-8
	scriptencoding utf-8
endif

" What to save for views and sessions:
set viewoptions=folds,cursor,curdir,slash,unix
set sessionoptions=curdir,help,tabpages,winsize

if has('mac')
	let g:clipboard = {
				\   'name': 'macOS-clipboard',
				\   'copy': {
				\      '+': 'pbcopy',
				\      '*': 'pbcopy',
				\    },
				\   'paste': {
				\      '+': 'pbpaste',
				\      '*': 'pbpaste',
				\   },
				\   'cache_enabled': 0
				\ }
endif

if has('clipboard')
	set clipboard& clipboard+=unnamedplus
endif

" Wildmenu {{{
" --------
if has('wildmenu')
	if ! has('nvim')
		set wildmode=list:longest
	endif

	" if has('nvim')
	"		set wildoptions=pum
	" else
	"		set nowildmenu
	"		set wildmode=list:longest,full
	"		set wildoptions=tagfile
	" endif
	set wildignorecase
	set wildignore+=.git,.hg,.svn,.stversions,*.pyc,*.spl,*.o,*.out,*~,%*
	set wildignore+=*.jpg,*.jpeg,*.png,*.gif,*.zip,**/tmp/**,*.DS_Store
	set wildignore+=**/node_modules/**,**/bower_modules/**,*/.sass-cache/*
	set wildignore+=application/vendor/**,**/vendor/ckeditor/**,media/vendor/**
	set wildignore+=__pycache__,*.egg-info,.pytest_cache,.mypy_cache/**
	set wildcharm=<C-z>  " substitue for 'wildchar' (<Tab>) in macros
endif
" }}}

" Vim Directories {{{
" ---------------
set nobackup
set nowritebackup
set undofile noswapfile
set directory=$DATA_PATH/swap//,$DATA_PATH,~/tmp,/var/tmp,/tmp
set undodir=$DATA_PATH/undo//,$DATA_PATH,~/tmp,/var/tmp,/tmp
set backupdir=$DATA_PATH/backup/,$DATA_PATH,~/tmp,/var/tmp,/tmp
set viewdir=$DATA_PATH/view/
" Use the coc-spell-checker to do this
" set spellfile=$VIM_PATH/spell/en.utf-8.add

" History saving
set history=2000

if has('nvim') && ! has('win32') && ! has('win64')
	set shada=!,'300,<50,@100,s10,h
else
	set viminfo='300,<10,@50,h,n$DATA_PATH/viminfo
endif

augroup user_persistent_undo
	autocmd!
	au BufWritePre /tmp/*          setlocal noundofile
	au BufWritePre COMMIT_EDITMSG  setlocal noundofile
	au BufWritePre MERGE_MSG       setlocal noundofile
	au BufWritePre *.tmp           setlocal noundofile
	au BufWritePre *.bak           setlocal noundofile
augroup END

" If sudo, disable vim swap/backup/undo/shada/viminfo writing
if $SUDO_USER !=# '' && $USER !=# $SUDO_USER
			\ && $HOME !=# expand('~'.$USER)
			\ && $HOME ==# expand('~'.$SUDO_USER)

	set noswapfile
	set nobackup
	set noundofile
	if has('nvim')
		set shada="NONE"
	else
		set viminfo="NONE"
	endif
endif

" Secure sensitive information, disable backup files in temp directories
if exists('&backupskip')
	set backupskip+=/tmp/*,$TMPDIR/*,$TMP/*,$TEMP/*,*/shm/*,/private/var/*
	set backupskip+=.vault.vim
endif

" Disable swap/undo/viminfo/shada files in temp directories or shm
augroup user_secure
	autocmd!
	silent! autocmd BufNewFile,BufReadPre
				\ /tmp/*,$TMPDIR/*,$TMP/*,$TEMP/*,*/shm/*,/private/var/*,.vault.vim
				\ setlocal noswapfile noundofile nobackup nowritebackup viminfo= shada=
augroup END

" }}}

" Tabs and Indents {{{
" ----------------
set textwidth=80    " Text width maximum chars before wrapping
set noexpandtab     " Don't expand tabs to spaces
set tabstop=2       " The number of spaces a tab is
set shiftwidth=2    " Number of spaces to use in auto(indent)
set softtabstop=-1  " Automatically keeps in sync with shiftwidth
set smarttab        " Tab insert blanks according to 'shiftwidth'
set autoindent      " Use same indenting on new lines
set smartindent     " Smart autoindenting on new lines
set shiftround      " Round indent to multiple of 'shiftwidth'

if exists('&breakindent')
	set breakindentopt=shift:2,min:20
endif

" }}}

" Timing {{{
" ------
set timeout ttimeout
set timeoutlen=500   " Time out on mappings
set ttimeoutlen=10   " Time out on key codes
set updatetime=100   " Idle time to write swap and trigger CursorHold
set redrawtime=1500  " Time in milliseconds for stopping display redraw

" }}}

" Searching {{{
" ---------
set ignorecase    " Search ignoring case
set smartcase     " Keep case when searching with *
set infercase     " Adjust case in insert completion mode
set incsearch     " Incremental search
set wrapscan      " Searches wrap around the end of the file
set hlsearch      " Highlight search results

set complete=.,w,b,k  " C-n completion: Scan buffers, windows and dictionary

if exists('+inccommand')
	set inccommand=nosplit
endif

if executable('rg')
	set grepformat=%f:%l:%m
	let &grepprg = 'rg --vimgrep' . (&smartcase ? ' --smart-case' : '')
elseif executable('ag')
	set grepformat=%f:%l:%m
	let &grepprg = 'ag --vimgrep' . (&smartcase ? ' --smart-case' : '')
endif

" }}}

" Behavior {{{
" --------
set autoread                    " Auto readfile
set nowrap                      " No wrap by default
set linebreak                   " Break long lines at 'breakat'
set breakat=\ \	;:,!?           " Long lines break chars
set nostartofline               " Cursor in same column for few commands
set whichwrap+=h,l,<,>,[,],~    " Move to following line on certain keys
set splitbelow splitright       " Splits open bottom right
set switchbuf=useopen,vsplit    " Jump to the first open window
set backspace=indent,eol,start  " Intuitive backspacing in insert mode
set diffopt=filler,iwhite       " Diff mode: show fillers, ignore whitespace
set completeopt=menu,menuone    " Always show menu, even for one item
set completeopt+=noselect,noinsert

if &compatible
  set nocompatible
endif
" Add the dein installation directory into runtimepath
set runtimepath+=~/.cache/dein/repos/github.com/Shougo/dein.vim

if dein#load_state('~/.cache/dein')
  call dein#begin('~/.cache/dein')

  call dein#add('~/.cache/dein/repos/github.com/Shougo/dein.vim')
  call dein#add('Shougo/deoplete.nvim')
  if !has('nvim')
    call dein#add('roxma/nvim-yarp')
    call dein#add('roxma/vim-hug-neovim-rpc')
  endif

  call dein#end()
  call dein#save_state()
endif

filetype plugin indent on
syntax enable

if exists('+completepopup')
	set completeopt+=popup
	set completepopup=height:4,width:60,highlight:InfoPopup
endif

" Use the new Neovim :h jumplist-stack
if has('nvim-0.5')
	set jumpoptions=stack
endif

if has('patch-8.1.0360') || has('nvim-0.4')
	set diffopt+=internal,algorithm:patience
	" set diffopt=indent-heuristic,algorithm:patience
endif
" }}}

" Editor UI {{{
set termguicolors       " Enable true color
set number              " Show number
set relativenumber      " Show relative number
set noshowmode          " Don't show mode on bottom
set noruler             " Disable default status ruler
set shortmess=aFc
set scrolloff=2         " Keep at least 2 lines above/below
set fillchars+=vert:\|  " add a bar for vertical splits
set fcs=eob:\           " hide ~ tila
set list
set listchars=tab:»·,nbsp:+,trail:·,extends:→,precedes:←
set title
" Title length.
set titlelen=95
" Title string.
let &g:titlestring="
			\ %{expand('%:p:~:.')}%(%m%r%w%)
			\ %<\[%{fnamemodify(getcwd(), ':~')}\] - Neovim"

set showmatch           " Jump to matching bracket
set matchpairs+=<:>     " Add HTML brackets to pair matching
set matchtime=1         " Tenths of a second to show the matching paren

set showtabline=2       " Always show the tabs line
set winwidth=30         " Minimum width for active window
set winminwidth=10      " Minimum width for inactive windows
" set winheight=4         " Minimum height for active window
set winminheight=1      " Minimum height for inactive window
set pumheight=15        " Pop-up menu's line height
set helpheight=12       " Minimum help window height
set previewheight=12    " Completion preview height

set showcmd             " Show command in status line
set noequalalways       " Don't resize windows on split or close
set laststatus=2        " Always show a status line
set display=lastline

if has('folding') && has('vim_starting')
	set foldenable
	set foldmethod=indent
	set foldlevelstart=99
endif

if has('nvim-0.4')
	set signcolumn=yes:1
else
	set signcolumn=yes           " Always show signs column
endif

if has('conceal') && v:version >= 703
	" For snippet_complete marker
	set conceallevel=2 concealcursor=niv
endif

if exists('+previewpopup')
	set previewpopup=height:10,width:60
endif

if &termguicolors
	if exists('&pumblend')
		set pumblend=10
	endif
	if exists('&winblend')
		set winblend=10
	endif
endif

au BufReadPost * if line("'\"") > 1 && line("'\"") <= line("$") | exe "normal! g'\"" | endif
" }}},
"记住上次编辑的位置
"au BufReadPost * if line("'\"") > 0|if line("'\"") <= line("$")|exe("norm '\"")|else|exe "norm $"|endif|endif
autocmd BufReadPost *
\ if line("'\"") > 0 && line("'\"") <= line("$") |
\ exe "normal g`\"" |
\ endif
