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

-- TOML 基本字符串里的反斜杠要转义（vsscript.dll 侧逐字符反转义，单反斜杠会被吞掉）
local function toml_escape(path)
    return (path:gsub("\\", "\\\\"))
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

-- 解析一行 TOML 的键（形如 "c:\\dir\\vsscript.dll" = [...]，返回值仍是转义形态）
local function line_key(line)
    return line:match('^%s*"([^"]+)"')
end

-- 反转义：把 TOML 里的双反斜杠还原成单反斜杠
local function toml_unescape(text)
    return (text:gsub("\\\\", "\\"))
end

-- 从 dir 开始逐层上溯，找同时有 python.exe 的那一层（最多 depth 层）
local function find_root_upwards(dir, depth)
    local current = dir
    for _ = 1, depth do
        if not current or current == "" then
            return nil
        end
        if exists(current .. "\\python.exe") then
            return current
        end
        current = parent_dir(current)
    end
    return nil
end

-- 定位包根，顺序：
--   1. script-opts 显式指定（k7f_vs-root，冒烟测试与特殊部署用）；
--   2. 脚本自身位置（<root>\portable_config\scripts\k7f_vs.lua 上溯两层）；
--   3. 退一步看 ~~/（便携布局下 = <root>\portable_config）。
-- 注意 --no-config 时 ~~/ 为空、--script 直挂文件时 get_script_directory() 为 nil，
-- 所以第 2、3 条都可能落空，第 1 条是兜底。
local function resolve_root()
    local opt_root = mp.get_opt("root")
    if opt_root and opt_root ~= "" then
        local found = find_root_upwards(to_windows_path(opt_root), 2)
        if found then
            return found
        end
    end
    local script_dir = mp.get_script_directory and mp.get_script_directory()
    if script_dir and script_dir ~= "" then
        local root = find_root_upwards(to_windows_path(script_dir), 4)
        if root then
            return root
        end
    end
    local cfg = to_windows_path(mp.command_native({"expand-path", "~~/"}) or "")
    if cfg == "" then
        return nil
    end
    return find_root_upwards(cfg, 2)
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
    local escaped_key = toml_escape(key)
    local wanted = string.format('"%s" = ["%s","%s","%s"]',
        escaped_key, toml_escape(python_exe), toml_escape(python_dll), mtime)

    -- 读旧配置：同键同 mtime 已就绪就什么都不做
    local lines = read_lines(runtime_cfg_path)
    for _, line in ipairs(lines) do
        if line_key(line) == escaped_key and line:find(mtime, 1, true) then
            msg.verbose("k7f_vs: 运行时配置已就绪")
            return
        end
    end

    -- 保留其它条目，但丢掉指向已消失目录的陈旧行（包被移动/删除后自动清理）
    local kept = {}
    for _, line in ipairs(lines) do
        local existing_key = line_key(line)
        if existing_key ~= escaped_key then
            if not existing_key or exists(toml_unescape(existing_key)) then
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

-- 脚本在文件加载之前完成，所以这里同步执行，保证 VapourSynth 初始化时配置已就位
bootstrap()
