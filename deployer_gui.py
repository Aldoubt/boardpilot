#!/usr/bin/env python3
"""Sunray SSH source deployment desktop. Launch from Windows or Ubuntu."""
import argparse
import codecs
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk,filedialog,messagebox,font
from pathlib import Path

from deployer_core import (SSHSession,UnknownHost,Cancelled,inspect_workspace,
                           package_workspace,sha256_file)

ANSI=re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')
APP_VERSION='0.1.2'


class App:
    def __init__(self,root):
        self.root=root;root.title('BoardPilot '+APP_VERSION+' · 板载部署助手')
        width=min(1480,root.winfo_screenwidth()-70);height=min(1080,root.winfo_screenheight()-100)
        root.geometry('%dx%d+35+35'%(width,height));root.minsize(min(1060,width),min(760,height))
        base=Path(os.environ.get('APPDATA',str(Path.home()/'.config')))
        self.config_dir=base/'BoardPilot';self.config_file=self.config_dir/'profile.json'
        try:self.config=json.loads(self.config_file.read_text(encoding='utf-8'))
        except (OSError,ValueError):self.config={}
        self.events=queue.Queue();self.cancel=threading.Event();self.busy=False;self.current_channel=None
        self.shell=None;self.shell_thread=None;self.stage=None;self.preview=None
        self.last_job=self.config.get('last_job');self.package=self.config.get('package')
        self.worker_buttons=[];self.last_progress=0;self.speed_start=None
        self.session=SSHSession(self.config_dir,self.log)
        def value(k,default):return str(self.config.get(k,default))
        self.host=tk.StringVar(value=value('host',''));self.port=tk.StringVar(value=value('port','22'))
        self.user=tk.StringVar(value=value('user',''));self.password=tk.StringVar();self.key=tk.StringVar(value=value('key',''))
        default_root=next((p for p in Path(__file__).resolve().parents if (p/'build.sh').is_file()),Path.home()/'Sunray_v2')
        self.workspace=tk.StringVar(value=value('workspace',str(default_root)))
        self.output=tk.StringVar(value=value('output',str(Path.home()/'BoardPilotments')))
        self.archive=tk.StringVar(value=self.package['path'] if self.package else '')
        self.incoming=tk.StringVar(value=value('incoming','~/boardpilot_incoming'));self.target=tk.StringVar(value=value('target','~/Sunray_v2'))
        self.modules=tk.StringVar(value=value('modules','livox_ros_driver2 ekf_odometry'));self.jobs=tk.StringVar(value=value('jobs','2'))
        self.status=tk.StringVar(value='就绪 · 先检查源码或连接板载');self.connection=tk.StringVar(value='● 尚未连接')
        self.package_info=tk.StringVar(value='选择 Git 工作区，生成仅包含源码的部署包。')
        self.stage_info=tk.StringVar(value='尚未上传。上传后的源码会先解压到独立暂存目录。')
        self.progress_text=tk.StringVar(value='等待操作');self.input=tk.StringVar();self.hide_input=tk.BooleanVar(value=False)
        families=font.families();self.face=next((x for x in ('Microsoft YaHei UI','Microsoft YaHei','Noto Sans CJK SC') if x in families),'TkDefaultFont')
        self.zoom=tk.StringVar(value=str(self.config.get('zoom','125%')))
        self.font_bases={};self.scroll_canvases=[]
        self.style();self.layout();self.apply_zoom()
        self.target.trace_add('write',lambda *_:self.invalidate_preview())
        self.root.after(80,self.pump);self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.log('准备就绪。上传只写入暂存目录；预览不会停止服务。密码不会写入配置文件。')

    def style(self):
        style=ttk.Style();style.theme_use('clam')
        self.root.configure(bg='#eef2f7')
        style.configure('.',font=(self.face,10),background='#eef2f7',foreground='#172b43')
        style.configure('TFrame',background='#eef2f7');style.configure('Card.TFrame',background='white')
        style.configure('TLabel',background='#eef2f7');style.configure('Card.TLabel',background='white')
        style.configure('Title.TLabel',font=(self.face,18,'bold'),foreground='#102b49')
        style.configure('Subtitle.TLabel',foreground='#52677c')
        style.configure('TButton',padding=(12,8),background='#e1e9f3',borderwidth=0)
        style.map('TButton',background=[('active','#ccdced')])
        style.configure('Accent.TButton',background='#2464c5',foreground='white')
        style.map('Accent.TButton',background=[('active','#194fa4'),('disabled','#9eb9dc')],foreground=[('disabled','#eef3fa')])
        style.configure('TEntry',fieldbackground='white',padding=6)
        style.configure('TNotebook',background='#eef2f7',borderwidth=0)
        style.configure('TNotebook.Tab',padding=(19,10))
        style.map('TNotebook.Tab',background=[('selected','white')],foreground=[('selected','#175bb4')])
        style.configure('TProgressbar',background='#2464c5',troughcolor='#dce6f1')

    def button(self,parent,text,command,accent=False):
        b=ttk.Button(parent,text=text,command=command,style='Accent.TButton' if accent else 'TButton')
        self.worker_buttons.append(b);return b

    def field(self,parent,title,var,browse=None,show=None):
        row=ttk.Frame(parent);row.pack(fill='x',pady=6)
        ttk.Label(row,text=title,width=13).pack(side='left')
        entry=ttk.Entry(row,textvariable=var,show=show or '');entry.pack(side='left',fill='x',expand=True)
        if browse:ttk.Button(row,text='选择',command=browse,width=6).pack(side='left',padx=(7,0))
        return entry

    def wrapped_label(self,parent,**options):
        options.setdefault('wraplength',760)
        options.setdefault('justify','left')
        label=ttk.Label(parent,**options)
        label.bind('<Configure>',lambda event:label.configure(wraplength=max(100,event.width-4)))
        return label

    def scrollable(self,parent,padding=0):
        outer=ttk.Frame(parent)
        canvas=tk.Canvas(outer,background='#eef2f7',highlightthickness=0,width=1,height=1)
        scrollbar=ttk.Scrollbar(outer,orient='vertical',command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True)
        content=ttk.Frame(canvas,padding=padding)
        window=canvas.create_window(0,0,window=content,anchor='nw')
        content.bind('<Configure>',lambda _:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda event:canvas.itemconfigure(window,width=event.width))
        self.scroll_canvases.append(canvas)
        return outer,content

    def scroll_wheel(self,event):
        widget=self.root.winfo_containing(event.x_root,event.y_root)
        while widget is not None:
            if widget in self.scroll_canvases:
                if widget.yview()!=(0.0,1.0):
                    delta=(-1 if event.num==4 else 1) if event.num in (4,5) else (-1 if event.delta>0 else 1)
                    widget.yview_scroll(delta*3,'units')
                    return 'break'
                return
            widget=getattr(widget,'master',None)

    def layout(self):
        self.root.columnconfigure(0,weight=1);self.root.rowconfigure(1,weight=1)
        header=ttk.Frame(self.root,padding=(25,20,25,13));header.grid(row=0,column=0,sticky='ew')
        title=ttk.Frame(header);title.pack(side='left')
        ttk.Label(title,text='BoardPilot '+APP_VERSION,style='Title.TLabel').pack(anchor='w')
        ttk.Label(title,text='源码打包  /  SSH 传输  /  板载编译 · Sunray/ROS1 预设',style='Subtitle.TLabel').pack(anchor='w',pady=(5,0))
        ttk.Button(header,text='保存连接配置',command=self.save).pack(side='right')
        zoom_box=ttk.Combobox(header,textvariable=self.zoom,values=['100%','125%','150%'],state='readonly',width=7)
        zoom_box.pack(side='right',padx=12);zoom_box.bind('<<ComboboxSelected>>',lambda _:self.apply_zoom(True))
        ttk.Label(header,text='界面缩放').pack(side='right')
        body=ttk.Frame(self.root,padding=(20,0,20,12));body.grid(row=1,column=0,sticky='nsew')
        sidebar=ttk.Frame(body,width=350);sidebar.pack(side='left',fill='y');sidebar.pack_propagate(False);self.sidebar=sidebar
        left_outer,left=self.scrollable(sidebar,padding=15);left_outer.pack(fill='both',expand=True)
        ttk.Label(left,text='板载连接',font=(self.face,13,'bold')).pack(anchor='w',pady=(0,10))
        for label,var in [('主机 IP',self.host),('SSH 端口',self.port),('用户名',self.user),('密码 / 密钥口令',self.password)]:
            ttk.Label(left,text=label).pack(anchor='w',pady=(8,3))
            ttk.Entry(left,textvariable=var,show='•' if var is self.password else '').pack(fill='x')
        ttk.Label(left,text='可选私钥文件').pack(anchor='w',pady=(10,3))
        kr=ttk.Frame(left);kr.pack(fill='x');ttk.Entry(kr,textvariable=self.key).pack(side='left',fill='x',expand=True)
        ttk.Button(kr,text='…',width=3,command=self.choose_key).pack(side='left',padx=(5,0))
        self.wrapped_label(left,text='密码仅用于本次连接，不落盘。',style='Subtitle.TLabel',wraplength=260).pack(fill='x',pady=10)
        self.button(left,'连接并检查环境',self.connect,True).pack(fill='x',pady=5)
        self.button(left,'断开 SSH',self.disconnect).pack(fill='x',pady=5)
        self.wrapped_label(left,textvariable=self.connection,foreground='#2563a0',wraplength=260).pack(fill='x',pady=12)
        ttk.Separator(left).pack(fill='x',pady=10)
        self.button(left,'打开交互式 SSH 终端',self.open_shell).pack(fill='x',pady=5)
        self.tabs=ttk.Notebook(body);self.tabs.pack(side='left',fill='both',expand=True,padx=(12,0))
        source_outer,source=self.scrollable(self.tabs,padding=22)
        deploy_outer,deploy=self.scrollable(self.tabs,padding=22)
        build_tab=ttk.Frame(self.tabs)
        terminal=ttk.Frame(self.tabs,padding=12)
        self.tabs.add(source_outer,text='1  源码打包');self.tabs.add(deploy_outer,text='2  上传与部署')
        self.tabs.add(build_tab,text='3  板载编译');self.tabs.add(terminal,text='4  日志与终端')
        self.build_tab=build_tab;self.terminal_tab=terminal
        self.root.bind_all('<MouseWheel>',self.scroll_wheel,add='+')
        self.root.bind_all('<Button-4>',self.scroll_wheel,add='+')
        self.root.bind_all('<Button-5>',self.scroll_wheel,add='+')
        ttk.Label(source,text='把当前修改打成可追溯的源码包',font=(self.face,14,'bold')).pack(anchor='w',pady=(0,12))
        self.field(source,'源码工作区',self.workspace,self.choose_workspace)
        self.field(source,'包输出目录',self.output,self.choose_output)
        row=ttk.Frame(source);row.pack(fill='x',pady=15)
        self.button(row,'检查分支与修改',self.inspect).pack(side='left')
        self.button(row,'生成源码部署包',self.pack,True).pack(side='left',padx=10)
        self.wrapped_label(source,textvariable=self.package_info,font=(self.face,11)).pack(fill='x',pady=15)
        ttk.Separator(source).pack(fill='x',pady=15)
        ttk.Label(source,text='打包规则',font=(self.face,12,'bold')).pack(anchor='w')
        self.wrapped_label(source,text='• 按 Git 跟踪与忽略规则收集源码，包含已检出的子模块。\n• 保留配置文件，排除构建产物、日志、Git 元数据和数据集。\n• Windows 的 Shell 脚本换行会转为 LF，保留 Git 可执行权限。\n• 包含分支、提交、未提交修改清单；生成 SHA256 校验值。').pack(fill='x',pady=12)
        self.field(source,'已有部署包',self.archive,self.choose_archive)
        self.button(source,'上传并校验、解压到暂存目录',self.upload,True).pack(anchor='w',pady=15)
        self.wrapped_label(deploy,text='先预览，再应用源码',font=(self.face,14,'bold')).pack(fill='x',pady=(0,12))
        self.field(deploy,'远程暂存目录',self.incoming)
        self.field(deploy,'目标工作区',self.target)
        self.wrapped_label(deploy,textvariable=self.stage_info,foreground='#45617d').pack(fill='x',pady=10)
        row=ttk.Frame(deploy);row.pack(fill='x',pady=8)
        self.button(row,'预览复制与删除计划',self.preview_install).pack(side='left')
        self.button(row,'备份并应用预览计划',self.apply_install,True).pack(side='left',padx=10)
        self.wrapped_label(deploy,text='安装沿用 apply_workspace.sh：保留被忽略的构建和依赖目录；覆盖同名源码，删除包内不存在的普通源码。应用前自动备份目标源码；安装后相关服务保持停止。',foreground='#6a4c22').pack(fill='x',pady=10)
        ttk.Button(deploy,text='下一步：板载编译',command=lambda:self.tabs.select(self.build_tab)).pack(anchor='w',pady=12)
        actions=ttk.Frame(build_tab,padding=(22,10,22,16));actions.pack(side='bottom',fill='x')
        self.build_button=self.button(actions,'在板载启动编译',self.build,True)
        self.build_button.pack(fill='x',pady=(0,6))
        self.follow_button=self.button(actions,'查看上次编译任务',self.follow_last)
        self.follow_button.pack(fill='x')
        build_outer,build=self.scrollable(build_tab,padding=22);build_outer.pack(fill='both',expand=True)
        self.wrapped_label(build,text='源码安装完成后，在 NX 上编译',font=(self.face,14,'bold')).pack(fill='x',pady=(0,12))
        self.wrapped_label(build,text='源码已经安装时，连接板载后即可编译，无需重新上传或再次应用源码。',style='Subtitle.TLabel').pack(fill='x',pady=(0,12))
        self.field(build,'目标工作区',self.target)
        self.field(build,'编译模块',self.modules)
        self.field(build,'并行任务数',self.jobs)
        self.wrapped_label(build,text='模块用空格分隔，例如 livox_ros_driver2 ekf_odometry。跨版本更新需要编译全部受影响模块及依赖。',style='Subtitle.TLabel').pack(fill='x',pady=7)
        self.wrapped_label(build,text='编译使用板载 build.sh，在后台记录日志；SSH 断开后继续运行。编译完成不会自动启动飞行节点。',style='Subtitle.TLabel').pack(fill='x',pady=7)
        toolbar=ttk.Frame(terminal);toolbar.pack(fill='x',pady=(0,8))
        ttk.Button(toolbar,text='清空显示',command=self.clear_log).pack(side='left')
        ttk.Button(toolbar,text='保存显示日志',command=self.save_log).pack(side='left',padx=8)
        ttk.Button(toolbar,text='发送 Ctrl+C',command=self.ctrl_c).pack(side='right')
        area=ttk.Frame(terminal);area.pack(fill='both',expand=True)
        self.console=tk.Text(area,bg='#101d2d',fg='#dbe9f9',insertbackground='white',font=('Consolas',10),wrap='word',relief='flat',state='disabled',padx=12,pady=12)
        scroll=ttk.Scrollbar(area,command=self.console.yview);self.console.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');self.console.pack(side='left',fill='both',expand=True)
        inputrow=ttk.Frame(terminal);inputrow.pack(fill='x',pady=(10,0))
        self.command_entry=ttk.Entry(inputrow,textvariable=self.input);self.command_entry.pack(side='left',fill='x',expand=True)
        self.command_entry.bind('<Return>',lambda _:self.send_input())
        ttk.Button(inputrow,text='发送',command=self.send_input).pack(side='left',padx=6)
        ttk.Checkbutton(inputrow,text='隐藏输入',variable=self.hide_input,command=lambda:self.command_entry.configure(show='•' if self.hide_input.get() else '')).pack(side='left')
        self.wrapped_label(terminal,text='交互终端或安装过程需要密码时，在远端密码提示出现后勾选“隐藏输入”再发送。',style='Subtitle.TLabel').pack(fill='x',pady=(7,0))
        footer=ttk.Frame(self.root,padding=(25,0,25,15));footer.grid(row=2,column=0,sticky='ew')
        ttk.Label(footer,textvariable=self.status).pack(anchor='w')
        row=ttk.Frame(footer);row.pack(fill='x',pady=(7,0))
        self.progress=ttk.Progressbar(row,maximum=100);self.progress.pack(side='left',fill='x',expand=True)
        self.stop_button=ttk.Button(row,text='停止当前操作 / 停止监看',command=self.stop,state='disabled');self.stop_button.pack(side='left',padx=(12,0))
        ttk.Label(footer,textvariable=self.progress_text,style='Subtitle.TLabel').pack(anchor='w',pady=(4,0))

    def apply_zoom(self,persist=False):
        try:scale=int(self.zoom.get().rstrip('%'))/100
        except ValueError:scale=1.25;self.zoom.set('125%')
        scale=max(1,min(1.5,scale))
        style=ttk.Style()
        style.configure('.',font=(self.face,round(10*scale)))
        style.configure('Title.TLabel',font=(self.face,round(18*scale),'bold'))
        def visit(widget):
            try:
                current=widget.cget('font')
                if current:
                    key=str(widget)
                    if key not in self.font_bases:self.font_bases[key]=font.Font(font=current).actual()
                    base=self.font_bases[key];widget.configure(font=(base['family'],max(1,round(base['size']*scale)),base['weight']))
            except tk.TclError:pass
            for child in widget.winfo_children():visit(child)
        visit(self.root)
        self.sidebar.configure(width=round(295*scale))
        width=min(round(1230*scale),self.root.winfo_screenwidth()-70)
        height=min(round(870*scale),self.root.winfo_screenheight()-100)
        self.root.geometry('%dx%d'%(width,height))
        if persist:self.save(False)

    def choose_key(self):
        value=filedialog.askopenfilename(title='选择 SSH 私钥')
        if value:self.key.set(value)

    def choose_workspace(self):
        value=filedialog.askdirectory(title='选择 Sunray Git 工作区')
        if value:self.workspace.set(value)

    def choose_output(self):
        value=filedialog.askdirectory(title='选择工作区之外的输出目录')
        if value:self.output.set(value)

    def choose_archive(self):
        value=filedialog.askopenfilename(title='选择源码部署包',filetypes=[('源码部署包','*.tar.gz')])
        if value:
            self.archive.set(value);self.package=None;self.stage=None;self.invalidate_preview()

    def log(self,text):self.events.put(('log',str(text)))

    def emit(self):
        decoder=codecs.getincrementaldecoder('utf-8')(errors='replace')
        return lambda data:self.events.put(('raw',decoder.decode(data)))

    def progress_update(self,done,total):
        now=time.monotonic()
        if now-self.last_progress>.12 or done==total:
            self.last_progress=now;self.events.put(('progress',done,total,now))

    def append(self,text,raw=False):
        text=ANSI.sub('',text).replace('\r','')
        self.console.configure(state='normal')
        self.console.insert('end',text if raw else '[%s] %s\n' % (time.strftime('%H:%M:%S'),text))
        if int(self.console.index('end-1c').split('.')[0])>15000:self.console.delete('1.0','3000.0')
        self.console.see('end');self.console.configure(state='disabled')

    def start(self,label,work,done=None):
        if self.busy:return
        self.busy=True;self.cancel.clear();self.status.set(label);self.progress['value']=0;self.speed_start=None
        for button in self.worker_buttons:button.configure(state='disabled')
        self.stop_button.configure(state='normal');self.log(label)
        def worker():
            try:result=work()
            except BaseException as exc:self.events.put(('failed',exc))
            else:self.events.put(('done',done,result))
        threading.Thread(target=worker,daemon=True).start()

    def finish(self):
        self.busy=False;self.current_channel=None
        for button in self.worker_buttons:button.configure(state='normal')
        self.stop_button.configure(state='disabled')
        if self.session.client and self.session.client.get_transport().is_active():
            self.connection.set('● 已连接 %s@%s' % (self.session.identity[2],self.session.identity[0]))
        else:self.connection.set('● 尚未连接')

    def pump(self):
        for _ in range(150):
            try:event=self.events.get_nowait()
            except queue.Empty:break
            kind=event[0]
            if kind in ('log','raw'):self.append(event[1],kind=='raw')
            elif kind=='progress':
                _,done,total,now=event;self.progress['value']=100*done/max(1,total)
                if self.speed_start is None:self.speed_start=(done,now)
                self.progress_text.set('%s / %s  ·  %.1f%%' % (f'{done:,}',f'{total:,}',100*done/max(1,total)))
                if total>1024*1024:
                    speed=(done-self.speed_start[0])/max(.01,now-self.speed_start[1])/1024**2
                    self.progress_text.set('%.1f / %.1f MB  ·  %.2f MB/s' % (done/1024**2,total/1024**2,speed))
            elif kind=='done':
                self.finish();self.status.set('操作完成');self.progress['value']=100
                if event[1]:event[1](event[2])
            elif kind=='job':self.last_job=event[1];self.save(False)
            elif kind=='failed':
                self.finish();exc=event[1];self.status.set('已停止' if isinstance(exc,Cancelled) else '操作未完成')
                if isinstance(exc,UnknownHost):
                    trust=messagebox.askyesno('核对 SSH 主机指纹','首次连接此主机：\n%s\n\n请与板载/管理员提供的指纹核对。确认后保存此主机公钥？'%exc)
                    if trust:self.root.after(0,lambda e=exc:self.connect((e.hostname,e.key)))
                elif isinstance(exc,Cancelled):self.append('已停止当前操作。远程编译若已启动，会继续运行；可点击“查看上次编译任务”。')
                else:
                    self.append('错误：'+str(exc));messagebox.showerror('操作未完成',str(exc))
        self.root.after(80,self.pump)

    def save(self,notify=True):
        data={k:getattr(self,k).get() for k in ('host','port','user','key','workspace','output','incoming','target','modules','jobs')}
        data.update(package=self.package,last_job=self.last_job,zoom=self.zoom.get())
        self.config_dir.mkdir(parents=True,exist_ok=True)
        self.config_file.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        if notify:self.log('配置已保存（不含密码）：'+str(self.config_file))

    def require_connection(self):
        self.session.require()
        identity=(self.host.get().strip(),int(self.port.get()),self.user.get().strip())
        if self.session.identity!=identity:raise ValueError('连接信息已修改，请先重新连接该主机')

    def guard(self,fn):
        try:return fn()
        except Exception as exc:messagebox.showerror('检查输入',str(exc));return None

    def connect(self,trusted=None):
        host=self.host.get().strip();port=self.port.get().strip();user=self.user.get().strip();password=self.password.get();key=self.key.get().strip()
        def work():
            self.session.connect(host,port,user,password,key,trusted)
            self.log('SSH 已连接，正在读取板载环境。')
            return self.session.environment(self.cancel)
        def done(text):self.append(text);self.stage=None;self.invalidate_preview();self.save(False);self.tabs.select(self.terminal_tab)
        self.start('连接板载并检查环境…',work,done)

    def disconnect(self):
        self.shell=None;self.session.close();self.connection.set('● 已断开');self.stage=None;self.invalidate_preview();self.log('SSH 已断开')

    def inspect(self):
        root=self.workspace.get()
        def done(info):
            self.package_info.set('分支：%s\n提交：%s\n源码文件：%s\n工作区：%s' % (info['branch'],info['commit'],len(info['files']),'有未提交修改（会一起打包）' if info['status'] else '干净'))
            self.append(info['status'] or '工作区没有未提交修改。')
        self.start('检查源码工作区…',lambda:inspect_workspace(root),done)

    def pack(self):
        root=self.workspace.get();output=self.output.get()
        def done(result):
            self.package=result;self.archive.set(result['path']);self.stage=None;self.invalidate_preview()
            self.package_info.set('已生成：%s\n提交：%s\n大小：%.1f MB\nSHA256：%s' % (Path(result['path']).name,result['commit'][:12],result['bytes']/1024**2,result['sha256']))
            self.save(False)
        self.start('正在打包源码…',lambda:package_workspace(root,output,self.log,self.progress_update,self.cancel),done)

    def upload(self):
        if not self.guard(lambda:(self.require_connection(),True)[1]):return
        archive=self.archive.get().strip();base=self.incoming.get()
        if not Path(archive).is_file():messagebox.showerror('选择部署包','请先生成或选择 .tar.gz 源码包');return
        def work():
            uploaded=self.session.upload(archive,base,self.progress_update,self.cancel)
            self.log('上传校验一致，解压到独立暂存目录…')
            return self.session.extract(uploaded,self.cancel)
        def done(stage):
            self.stage=stage;self.invalidate_preview()
            manifest=stage.get('manifest',{})
            self.stage_info.set('暂存源码：%s\n源提交：%s\n校验：%s' % (stage['source'],manifest.get('commit','包内未记录'),stage['sha256']))
            self.log('暂存就绪，尚未修改目标工作区：'+stage['source']);self.tabs.select(1)
        self.start('上传部署包（支持同一文件断点续传）…',work,done)

    def invalidate_preview(self):self.preview=None

    def preview_install(self):
        if not self.guard(lambda:(self.require_connection(),True)[1]):return
        if not self.stage:messagebox.showinfo('尚未上传','请先上传并解压部署包');return
        target=self.target.get();stage=self.stage.copy()
        command=self.guard(lambda:self.session.apply_command(stage,target,True))
        if not command:return
        def work():
            code,text=self.session.command(command,self.cancel,emit=self.emit(),timeout=180)
            if code:raise RuntimeError('预览失败：\n'+text[-6000:])
            return text
        def done(text):
            self.preview=(stage['source'],self.session.path(target),self.session.identity,text)
            self.log('预览完成。应用前会备份目标源码，再执行上述复制/删除计划。')
        self.tabs.select(self.terminal_tab);self.start('预览安装计划（不停止服务）…',work,done)

    def apply_install(self):
        if not self.guard(lambda:(self.require_connection(),True)[1]):return
        if not self.stage or not self.preview:messagebox.showinfo('先预览','请先预览当前包和目标目录的安装计划');return
        target=self.target.get();stage=self.stage.copy();base=self.incoming.get()
        key=(stage['source'],self.session.path(target),self.session.identity)
        if self.preview[:3]!=key:messagebox.showinfo('重新预览','目标或连接已变化，请重新预览');return
        detail='\n'.join(x for x in self.preview[3].splitlines() if '[apply]' in x)[-3000:]
        if not messagebox.askyesno('应用已预览的源码变更',detail+'\n\n将先备份目标源码（不含编译产物），再覆盖/删除源码并停止相关服务。完成后服务保持停止。确定执行？'):return
        command=self.session.apply_command(stage,target,False)
        def work():
            self.log('备份目标源码…')
            backup=self.session.backup(target,base,self.cancel,source=stage['source']);self.log('备份：'+str(backup.get('backup') or '目标尚不存在'))
            code,text=self.session.command(command,self.cancel,emit=self.emit(),on_channel=self.set_channel,timeout=300)
            if code:raise RuntimeError('安装未完成；服务可能已停止，目标可能部分更新。\n'+text[-5000:])
            return text
        def done(_):self.invalidate_preview();self.log('源码安装完成。接下来编译；服务没有自动启动。')
        self.tabs.select(self.terminal_tab);self.start('备份并应用源码变更…',work,done)

    def set_channel(self,channel):self.current_channel=channel

    def build(self):
        if not self.guard(lambda:(self.require_connection(),True)[1]):return
        target=self.target.get();modules=self.modules.get();jobs=self.jobs.get();base=self.incoming.get()
        if not messagebox.askyesno('确认板载编译','目标：%s\n模块：%s\n并行数：%s\n\n仅编译，不会自动启动节点。确认受影响节点已停止后继续。'%(target,modules,jobs)):return
        def work():
            job=self.session.start_build(target,modules,jobs,base)
            self.events.put(('log','后台编译 PID=%s，日志=%s' % (job['pid'],job['log'])))
            self.events.put(('job',job))
            return self.session.follow_build(job,self.emit(),self.cancel)
        self.tabs.select(self.terminal_tab);self.start('板载后台编译并监看日志…',work,self.build_done)

    def build_done(self,status):
        self.log('编译成功（退出码0）；请先做静止验证，再按需要启动模块。' if status==0 else '编译失败，退出码%s，请查看日志。'%status)
        self.status.set('编译成功' if status==0 else '编译失败')

    def follow_last(self):
        if not self.guard(lambda:(self.require_connection(),True)[1]):return
        if not self.last_job:messagebox.showinfo('没有任务','尚未记录后台编译任务');return
        job=self.last_job.copy();self.tabs.select(self.terminal_tab)
        self.start('读取后台编译日志…',lambda:self.session.follow_build(job,self.emit(),self.cancel),self.build_done)

    def open_shell(self):
        if not self.guard(lambda:(self.require_connection(),True)[1]):return
        if self.shell and not self.shell.closed:self.tabs.select(self.terminal_tab);return
        try:self.shell=self.session.client.invoke_shell(width=140,height=36)
        except Exception as exc:messagebox.showerror('SSH终端',str(exc));return
        channel=self.shell;emit=self.emit()
        def reader():
            try:
                while not channel.closed:
                    data=channel.recv(65536)
                    if not data:break
                    emit(data)
            except Exception as exc:self.log('SSH终端已断开：'+str(exc))
        self.shell_thread=threading.Thread(target=reader,daemon=True);self.shell_thread.start()
        self.tabs.select(self.terminal_tab);self.command_entry.focus_set()

    def send_input(self):
        channel=self.current_channel or self.shell
        if not channel or channel.closed:messagebox.showinfo('没有交互连接','请先打开 SSH 终端，或等待安装过程出现密码提示');return
        try:channel.sendall(self.input.get()+'\n');self.input.set('')
        except Exception as exc:messagebox.showerror('发送失败',str(exc))

    def ctrl_c(self):
        channel=self.current_channel or self.shell
        if channel and not channel.closed:channel.sendall('\x03')

    def stop(self):
        self.cancel.set();self.log('正在停止本地操作/日志监看。后台编译不会被终止。')

    def clear_log(self):self.console.configure(state='normal');self.console.delete('1.0','end');self.console.configure(state='disabled')

    def save_log(self):
        path=filedialog.asksaveasfilename(defaultextension='.log',initialfile='boardpilot_'+time.strftime('%Y%m%d_%H%M%S')+'.log')
        if path:Path(path).write_text(self.console.get('1.0','end'),encoding='utf-8')

    def close(self):
        if self.busy and not messagebox.askyesno('关闭窗口','当前操作尚未完成。关闭将中断打包/传输或安装监看，后台编译会继续。确定关闭？'):return
        self.cancel.set();self.save(False);self.session.close();self.root.destroy()


def main():
    if os.name=='nt':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError,OSError):pass
    parser=argparse.ArgumentParser();parser.add_argument('--smoke-test',action='store_true');parser.add_argument('--screenshot');parser.add_argument('--smoke-tab',type=int,default=0)
    args=parser.parse_args();root=tk.Tk()
    if os.name=='nt':root.tk.call('tk','scaling',1.35)
    app=App(root)
    if args.smoke_test:
        app.tabs.select(args.smoke_tab)
        def finish():
            if args.screenshot:
                from PIL import ImageGrab
                root.update();x=root.winfo_rootx();y=root.winfo_rooty()
                ImageGrab.grab(bbox=(x,y,x+root.winfo_width(),y+root.winfo_height())).save(args.screenshot)
            root.destroy()
        root.after(900,finish)
    root.mainloop()


if __name__=='__main__':main()
