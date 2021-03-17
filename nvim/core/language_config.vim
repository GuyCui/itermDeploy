" 语言设置

" 这里嘛。。是用来一键编译代码的
noremap <C-r> :call CompileRunGcc()<CR>
func! CompileRunGcc()
  exec "w"
  if &filetype == 'java'
    exec "!javac %"
    exec "!time java %<"
  elseif &filetype == 'sh'
    :!time bash %
  elseif &filetype == 'python'
    set splitbelow
    :sp
    :term python3 %
  elseif &filetype == 'html'
    silent! exec "!" google-chrome-stable " % &"
  elseif &filetype == 'markdown'
    exec "InstantMarkdownPreview"
  elseif &filetype == 'tex'
    silent! exec "VimtexStop"
    silent! exec "VimtexCompile"
  elseif &filetpe == 'dart'
    exec "CocCommand flutter.run -d ".g:flutter_default_device
    CocCommand flutter.dev.openDevLog
  elseif &filetype == 'javascript'
    set splitbelow
    :sp
    :term export DEBUG="INFO,ERROR,WARNING"; node --trace-warnings .
  elseif &filetype == 'go'
    set splitbelow
    :sp
    :term go run .
  endif
endfunc

" 每次创建Java文件都会初始化一些内容
autocmd BufNewFile *.java exec ":call CppInit()"
func CppInit()
  if expand("%:e") == "java"
    call setline(1,"/*")
    call setline(2,"*******************************************************************")
    call setline(3,"Author:                GuyCui")
    call setline(4,"Date:                  ".strftime("%Y-%m-%d"))
    call setline(5,"FileName：             ".expand("%"))
    call setline(6,"Copyright (C):         ".strftime("%Y")." All rights reserved")
    call setline(7,"*******************************************************************")
    " call setline(8,"*/")
    " call setline(9,"#include<iostream>")
    " call setline(10,"")
    " call setline(11,"int main(int argc, const char *argv[]){")
    " call setline(12,"")
    " call setline(13,"  return 0;")
    " call setline(14,"}")
  endif
endfunc

autocmd BufNewFile * normal G'y
