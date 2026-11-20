--[[
k7f_playkit.lua —— PlayKit 预设前端（本 fork 新增）

职责边界：这里只负责把 mpv 的按键/消息翻译成一次 vapoursynth 滤镜切换，
真正的滤镜编排在 k7sfunc/mod_playkit.py 的 PLAYKIT_* 函数里，
可直接在 vpy 中调用，也可由本脚本生成临时 vpy 加载。

可用命令（input.conf 或控制台）：
  <KEY>  script-binding k7f_playkit/toggle         # 按当前配置开启/关闭
  <KEY>  script-binding k7f_playkit/off            # 强制关闭
         script-message-to k7f_playkit apply
         script-message-to k7f_playkit set-preset anime
         script-message-to k7f_playkit set-key target_h 1440
         script-message-to k7f_playkit status

配置：portable_config/script-opts/k7f_playkit.conf
]]

local mp = require("mp")
local msg = require("mp.msg")

local opt = {
	cache_dir = "~~/",        -- 生成的 vpy 存放目录
	preset = "auto",          -- auto/upscale/memc/denoise/deband/clean/anime/live
	target_h = 0,             -- 0 = 跟随屏幕高度
	engine = "auto",          -- 超分引擎：auto/artcnn/acnet/cugan/edi/ngu/none
	memc_engine = "auto",     -- 插帧引擎：auto/rife_nv/rife_dml/none
	memc_fps = 60,            -- 插帧目标帧率
	denoise_lv = 3.0,         -- NLM 降噪强度
	deband_rth = 48.0,        -- 去色带阈值
	gpu = 0,
	gpu_m = 1,
	gpu_t = 2,
}

local opt_number = {
	target_h = true,
	memc_fps = true,
	denoise_lv = true,
	deband_rth = true,
	gpu = true,
	gpu_m = true,
	gpu_t = true,
}

mp.options.read_options(opt, "k7f_playkit", function(changed)
	for key in pairs(changed) do
		if opt_number[key] then
			opt[key] = tonumber(opt[key]) or opt[key]
		end
	end
end)

local LABEL = "PLAYKIT"

local function active_path()
	return mp.command_native({"expand-path", opt.cache_dir}) .. "k7f_playkit.vpy"
end

local function target_height()
	local target = tonumber(opt.target_h) or 0
	if target > 0 then
		return target
	end
	local disp = mp.get_property_number("display-height", 1080)
	return math.floor(disp or 1080)
end

-- 生成最小 vpy：真正逻辑都在 mod_playkit 里，这里只是把参数转成一次调用
local function build_vpy(target)
	local lines = {
		"# 由 k7f_playkit.lua 生成，改参数请改 script-opts/k7f_playkit.conf",
		"import vapoursynth as vs",
		"import k7sfunc as k7f",
		'fps_in = globals().get("container_fps", 24.0)',
		string.format(
			'clip = k7f.PLAYKIT_RUN(video_in, preset="%s", target_h=%d, engine="%s", memc_engine="%s", memc_fps=%s, fps_in=fps_in, denoise_lv=%s, deband_rth=%s, gpu=%d, gpu_m=%d, gpu_t=%d)',
			opt.preset, target, opt.engine, opt.memc_engine, tostring(opt.memc_fps),
			tostring(opt.denoise_lv), tostring(opt.deband_rth), opt.gpu, opt.gpu_m, opt.gpu_t),
		"clip.set_output()",
	}
	return table.concat(lines, "\n") .. "\n"
end

local function filter_on()
	local vf = mp.get_property_native("vf") or {}
	for _, f in ipairs(vf) do
		if f.label == LABEL then
			return true
		end
	end
	return false
end

local function remove_filter()
	if filter_on() then
		mp.commandv("vf", "remove", "@" .. LABEL)
	end
end

local function apply()
	local target = target_height()
	local path = active_path()
	local fh, err = io.open(path, "w")
	if not fh then
		msg.warn("无法写入 " .. path .. ": " .. tostring(err))
		mp.osd_message("playkit: 无法写入 vpy，详见日志", 3)
		return
	end
	local ok, write_err = fh:write(build_vpy(target))
	fh:close()
	if not ok then
		msg.warn("写入 vpy 失败: " .. tostring(write_err))
		return
	end
	remove_filter()
	mp.commandv("vf", "append", "@" .. LABEL .. ':vapoursynth="' .. path .. '"')
	mp.osd_message(string.format("playkit: %s → %dp", opt.preset, target), 2)
	msg.info(string.format("preset=%s target_h=%d engine=%s/%s",
		opt.preset, target, opt.engine, opt.memc_engine))
end

local function off()
	remove_filter()
	mp.osd_message("playkit: 已关闭", 2)
end

local function toggle()
	if filter_on() then
		off()
	else
		apply()
	end
end

mp.register_script_message("apply", apply)
mp.register_script_message("off", off)
mp.register_script_message("toggle", toggle)
mp.register_script_message("status", function()
	mp.osd_message(string.format("playkit: %s / %s / %s+%s",
		opt.preset, filter_on() and "已启用" or "未启用", opt.engine, opt.memc_engine), 3)
end)
mp.register_script_message("set-preset", function(value)
	opt.preset = value
	mp.osd_message("playkit preset = " .. tostring(value), 2)
	if filter_on() then
		apply()
	end
end)
mp.register_script_message("set-key", function(key, value)
	if key == nil or value == nil or opt[key] == nil then
		msg.warn("set-key 参数无效")
		return
	end
	if opt_number[key] then
		opt[key] = tonumber(value) or opt[key]
	else
		opt[key] = value
	end
	mp.osd_message(string.format("playkit %s = %s", key, tostring(opt[key])), 2)
end)

-- 与 mpv_PlayKit 既有脚本一致：默认不抢占按键，由用户在自己的 input.conf 绑定，例如
--   Ctrl+Shift+p  script-binding k7f_playkit/toggle
--   Ctrl+Shift+o  script-binding k7f_playkit/off
mp.add_key_binding(nil, "toggle", toggle)
mp.add_key_binding(nil, "off", off)
mp.add_key_binding(nil, "apply", apply)
