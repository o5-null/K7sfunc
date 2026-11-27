"""补帧 - MVtools

"""

import typing
import math
import vapoursynth as vs
from ._internal import (
	core,
	_validate_input_clip,
	_validate_bool,
	_validate_literal,
	_validate_numeric,
	_check_plugin,
)

##################################################
## MVtools补帧
##################################################

def MVT_LQ(
	input : vs.VideoNode,
	fps_in : float = 23.976,
	fps_out : float = 59.940,
	recal : bool = True,
	block : bool = True,
) -> vs.VideoNode :

	func_name = "MVT_LQ"
	_validate_input_clip(func_name, input)
	_validate_numeric(func_name, "fps_in", fps_in, min_val=0.0, exclusive_min=True)
	if not isinstance(fps_out, (int, float)) or fps_out <= 0.0 or fps_out <= fps_in :
		raise vs.Error(f"模块 {func_name} 的子参数 fps_out 的值无效")
	_validate_bool(func_name, "recal", recal)
	_validate_bool(func_name, "block", block)

	_check_plugin(func_name, "mv")

	w_in, h_in = input.width, input.height
	blk_size = 32
	w_tmp = math.ceil(w_in / blk_size) * blk_size - w_in
	h_tmp = math.ceil(h_in / blk_size) * blk_size - h_in
	if w_tmp + h_tmp > 0 :
		cut0 = core.std.AddBorders(clip=input, right=w_tmp, bottom=h_tmp)
	else :
		cut0 = input

	cut1 = core.std.AssumeFPS(clip=cut0, fpsnum=int(fps_in * 1e6), fpsden=1e6)
	cut_s = core.mv.Super(clip=cut1, pel=1, sharp=0)
	cut_b = core.mv.Analyse(super=cut_s, blksize=blk_size, search=2, isb=True)
	cut_f = core.mv.Analyse(super=cut_s, blksize=blk_size, search=2)

	if recal :
		cut_b = core.mv.Recalculate(super=cut_s, vectors=cut_b, thsad=200,
									blksize=blk_size / 2, search=2, searchparam=1)
		cut_f = core.mv.Recalculate(super=cut_s, vectors=cut_f, thsad=200,
									blksize=blk_size / 2, search=2, searchparam=1)
	else :
		cut_b, cut_f = cut_b, cut_f

	if block :
		output = core.mv.BlockFPS(clip=cut1, super=cut_s, mvbw=cut_b, mvfw=cut_f,
								  num=fps_out * 1e6, den=1e6)
	else :
		output = core.mv.FlowFPS(clip=cut1, super=cut_s, mvbw=cut_b, mvfw=cut_f,
								 num=fps_out * 1e6, den=1e6, mask=1)
	if w_tmp + h_tmp > 0 :
		output = core.std.Crop(clip=output, right=w_tmp, bottom=h_tmp)

	return output

##################################################
## MOD xvs (b24d5594206635f7373838acb80643d2ab141222)
## MVtools补帧
##################################################

def MVT_MQ(
	input : vs.VideoNode,
	fps_in : float = 23.976,
	fps_out : float = 59.940,
	qty_lv : typing.Literal[1, 2, 3] = 1,
	block : bool = True,
	blksize : typing.Literal[4, 8, 16, 32] = 8,
	thscd1 : int = 360,
	thscd2 : int = 80,
) -> vs.VideoNode :

	func_name = "MVT_MQ"
	_validate_input_clip(func_name, input)
	_validate_numeric(func_name, "fps_in", fps_in, min_val=0.0, exclusive_min=True)
	if not isinstance(fps_out, (int, float)) or fps_out <= 0.0 or fps_out <= fps_in :
		raise vs.Error(f"模块 {func_name} 的子参数 fps_out 的值无效")
	_validate_literal(func_name, "qty_lv", qty_lv, [1, 2, 3])
	_validate_bool(func_name, "block", block)
	_validate_literal(func_name, "blksize", blksize, [4, 8, 16, 32])
	_validate_numeric(func_name, "thscd1", thscd1, min_val=0, int_only=True)
	_validate_numeric(func_name, "thscd2", thscd2, min_val=0, max_val=255, int_only=True)

	_check_plugin(func_name, "mv")

	blksizev = blksize
	search = [0, 3, 3][(qty_lv - 1)]
	block_mode = [0, 0, 3][(qty_lv - 1)]
	flow_mask = [0, 1, 2][(qty_lv - 1)]
	analParams = { 'overlap':0,'overlapv':0,'search':search,'dct':0,'truemotion':True,
				   'blksize':blksize,'blksizev':blksizev,'searchparam':2,
				   'badsad':10000,'badrange':24,'divide':0 }
	bofp = { 'thscd1':thscd1,'thscd2':thscd2,'blend':True,'num':int(fps_out * 1e6),'den':1e6 }

	cut0 = core.std.AssumeFPS(input, fpsnum=int(fps_in * 1e6), fpsden=1e6)
	sup = core.mv.Super(input, pel=2, sharp=2, rfilter=4)
	bvec = core.mv.Analyse(sup, isb=True, **analParams)
	fvec = core.mv.Analyse(sup, isb=False, **analParams)
	if block == True :
		output =  core.mv.BlockFPS(cut0, sup, bvec, fvec, **bofp, mode=block_mode)
	else :
		output = core.mv.FlowFPS(cut0, sup, bvec, fvec, **bofp, mask=flow_mask)

	return output
