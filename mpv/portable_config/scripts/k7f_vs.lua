--[[
k7f_vs.lua —— 官方 VapourSynth（R82 及以上）便携化引导

背景：
  从 R82 起，vsscript.dll 不再在编译期链接 Python，而是在初始化时自行定位
  python.exe 与 python3.dll。定位顺序：
    1. 隐式布局探测：被加载的 vsscript.dll 往上四层目录里同时有 python.exe
       与 python3.dll，就直接用；
    2. 探测失败才读 %APPDATA%\vapoursynth\vapoursynth.toml，每行形如
       （键是 vsscript.dll 完整路径的**全小写**形式，逐字节比较，mtime 必须一致）：
         "c:\\games\\mpv\\vsscript.dll" = ["c:\\games\\mpv\\python.exe","c:\\games\\mpv\\python3.dll","1712345678"]

  本包的 mpv.exe 与 vsscript.dll 同在最外层，四层探测必然落空（会指向包的上一层
  甚至盘符根），所以首次运行需要补一条配置。本脚本只做这件事：只认本包布局
  （包根同时有 python.exe、python3.dll、vsscript.dll 与
  Lib\site-packages\vapoursynth\__init__.py），其它布局（例如自带 python 的旧包、
  python 与 dll 分层的布局）直接跳过，不干扰上游 mpv_PlayKit 的用户。

  注意：mpv 的 Lua 没有 os.setenv，无法用环境变量 VSSCRIPT_PATH 绕过这一步，
  因此写 TOML 是唯一"开箱即用"的路径。脚本不依赖任何配置项。

用法：放进 portable_config/scripts/，mpv 启动时自动加载。
--]]

local mp = require 'mp'
local msg = require 'mp.msg'
local utils = require 'mp.utils'

local runtime_cfg_dir = os.getenv("APPDATA") and (os.getenv("APPDATA") .. "\\vapoursynth") or nil
local runtime_cfg_path = runtime_cfg_dir and (runtime_cfg_dir .. "\\vapoursynth.toml") or nil

-- mpv 返回的路径可能混用分隔符、带重复或结尾分隔符；统一成 Windows 风格
local function to_windows_path(path)
    path = path:gsub("/", "\\")
    path = path:gsub("\\+", "\\")
    path = path:gsub("\\$", "")
    return path
end

local function parent_dir(path)
    return path:match("^(.*)\\[^\\]*$")
end

local function exists(path)
    return utils.file_info(path) ~= nil
end

-- 读取现有配置，返回行数组；文件不存在时返回空数组
local function read_lines(path)
    local f = io.open(path, "r")
    if not f then
        return {}
    end
    local lines = {}
    for line in f:lines() do
        lines[#lines + 1] = line
    end
    f:close()
    return lines
end

local function write_lines(path, lines)
    local f = io.open(path, "w")
    if not f then
        return false, "无法写入 " .. path
    end
    for _, line in ipairs(lines) do
        f:write(line, "\n")
    end
    f:close()
    return true
end

-- 解析一行 TOML 的键（形如 "c:\\dir\\vsscript.dll" = [...]）
local function line_key(line)
    return line:match('^%s*"([^"]+)"')
end

-- 定位包根：
--   便携布局下 ~~/ 就是 <root>\portable_config，需要回退一层；
--   个别情况下 ~~/ 已经指向包根，则直接用。
local function resolve_root()
    local cfg = to_windows_path(mp.command_native({"expand-path", "~~/"}) or "")
    if cfg == "" then
        return nil
    end
    if exists(cfg .. "\\python.exe") then
        return cfg
    end
    local parent = parent_dir(cfg)
    if parent and exists(parent .. "\\python.exe") then
        return parent
    end
    return nil
end

local function find_vsscript(root)
    for _, name in ipairs({"vsscript.dll", "VSScript.dll"}) do
        local candidate = root .. "\\" .. name
        if exists(candidate) then
            return candidate
        end
    end
    return nil
end

local function bootstrap()
    if not runtime_cfg_path then
        return
    end

    local root = resolve_root()
    if not root then
        msg.verbose("k7f_vs: 未找到包根，跳过")
        return
    end

    local vsscript = find_vsscript(root)
    local python_exe = root .. "\\python.exe"
    local python_dll = root .. "\\python3.dll"

    -- 只对"官方 Python embed + 官方 VapourSynth"布局生效
    if not vsscript or not exists(python_dll)
        or not exists(root .. "\\Lib\\site-packages\\vapoursynth\\__init__.py") then
        msg.verbose("k7f_vs: 非官方 Python/VapourSynth 布局，跳过")
        return
    end

    local info = utils.file_info(python_exe)
    if not info then
        return
    end
    local mtime = string.format("%d", math.floor(info.mtime))

    local key = string.lower(vsscript)
    local wanted = string.format('"%s" = ["%s","%s","%s"]', key, python_exe, python_dll, mtime)

    -- 读旧配置：同键同 mtime 已就绪就什么都不做
    local lines = read_lines(runtime_cfg_path)
    for _, line in ipairs(lines) do
        local existing_key = line_key(line)
        if existing_key == key and line:find(mtime, 1, true) then
            msg.verbose("k7f_vs: 运行时配置已就绪")
            return
        end
    end

    -- 保留其它条目，但丢掉指向已消失目录的陈旧行（包被移动/删除后自动清理）
    local kept = {}
    for _, line in ipairs(lines) do
        local existing_key = line_key(line)
        if existing_key ~= key then
            if not existing_key or exists(existing_key) then
                kept[#kept + 1] = line
            end
        end
    end
    kept[#kept + 1] = wanted

    os.execute('if not exist "' .. runtime_cfg_dir .. '" mkdir "' .. runtime_cfg_dir .. '" >nul 2>nul')
    local ok, err = write_lines(runtime_cfg_path, kept)
    if ok then
        msg.info("k7f_vs: 已写入 VapourSynth 运行时配置")
    else
        msg.warn("k7f_vs: " .. tostring(err))
    end
end

mp.add_timeout(0, bootstrap)
