import functools
import numpy as np
from .tensor import Function, GPUBuffer

def buffer_new(ctx, shape, zero=False):
  return GPUBuffer(shape, hostbuf=None if not zero else np.zeros(shape, dtype=np.float32))

def buffer_np(ctx, x):
  return ctx.thr.to_device(x)

@functools.lru_cache()
def clbuild(thr, prg, name):
  return thr.compile(prg).__getattr__(name)

def uint2(x, y):
  return np.array((x,y), dtype=cl.cltypes.uint2)
i32 = np.int32

# ************* unary ops *************

def unary_op(ctx, code, x):
  ret = buffer_new(ctx, x.shape)
  unop = clbuild(ctx.thr, """
  KERNEL void unop(GLOBAL_MEM const float *a_g, GLOBAL_MEM float *res_g) {
    SIZE_T gid = get_global_id(0);
    float a = a_g[gid];
    res_g[gid] = """ + code + """;
  }""", "unop")
  unop(x.cl, ret.cl, global_size=[int(np.prod(ret.shape))])
  return ret

class ReLU(Function):
  @staticmethod
  def forward(ctx, input):
    ctx.save_for_backward(input)
    return unary_op(ctx, 'max(a, (float)0.)', input)

  @staticmethod
  def backward(ctx, grad_output):
    input, = ctx.saved_tensors
    return binary_op(ctx, 'a * (b >= 0)', grad_output, input)

class Log(Function):
  @staticmethod
  def forward(ctx, input):
    ctx.save_for_backward(input)
    return unary_op(ctx, 'log(a)', input)

  @staticmethod
  def backward(ctx, grad_output):
    input, = ctx.saved_tensors
    return binary_op(ctx, 'a / b', grad_output, input)

class Exp(Function):
  @staticmethod
  def forward(ctx, input):
    ret = unary_op(ctx, 'exp(a)', input)
    ctx.save_for_backward(ret)
    return ret

  @staticmethod
  def backward(ctx, grad_output):
    ret, = ctx.saved_tensors
    return binary_op(ctx, 'a * b', grad_output, ret)

# ************* reduce ops *************

def reduce_op(ctx, code, code2, inp, axis=None, start="0.0"):
  if axis is None:
    # full reduce
    osize = [1]*len(inp.shape)
  else:
    osize = np.array(inp.shape)
    osize[list(axis)] = 1
  ret = buffer_new(ctx, osize)
  if axis is None:
    ret.shape = (1,)

  # TODO: this is insanely slow
  reduce = clbuild(ctx.thr, """
  KERNEL void reduce(GLOBAL_MEM const float *a_g, int sz, GLOBAL_MEM float *res_g, int prod, int n_dims,
                       GLOBAL_MEM const int *shape_x, GLOBAL_MEM const int *shape_ret) {
    SIZE_T gid = get_global_id(0);

    float out = """ + start + """;
    for (int x = 0; x < sz; x++) {
      int idx = 0;  // compute index into a_g
      int tprod = prod;
      int tsz = sz;
      for (int dim = 0; dim < n_dims; dim++) {
        idx *= shape_x[dim];
        if (shape_x[dim] == shape_ret[dim]) {   // dim from gid, don't reduce
          tprod /= shape_x[dim];
          idx += (gid / tprod) % shape_x[dim];
        } else {  // dim from x
          tsz /= shape_x[dim];
          idx += (x / tsz) % shape_x[dim];
        }
      }
      float a = a_g[idx];
      """ + code + """;
    }
    res_g[gid] = """ + code2 + """;
  }""", "reduce")
  reduce(inp.cl,
    i32(np.prod(inp.shape)//np.prod(osize)), ret.cl,
    i32(np.prod(osize)), i32(len(osize)),
    buffer_np(ctx, np.array(inp.shape, dtype=np.int32)),
    buffer_np(ctx, np.array(osize, dtype=np.int32)), global_size=[int(np.prod(osize))])
  return ret

class Sum(Function):
  @staticmethod
  def forward(ctx, input, axis=None):
    axis = [axis] if type(axis) == int else axis
    ctx.save_for_backward(input, axis)
    ret = reduce_op(ctx, "out += a", "out", input, axis=axis)
    if axis is not None:
      ret.shape = tuple([input.shape[i] for i in range(len(input.shape)) if i not in axis])
    return ret

  @staticmethod
  def backward(ctx, grad_output):
    input, axis = ctx.saved_tensors
    shape = [1 if axis is None or i in axis else input.shape[i] for i in range(len(input.shape))]
    output = GPUBuffer(shape, hostbuf=grad_output)
    return binary_op(ctx, 'a+b', output, buffer_new(ctx, input.shape, zero=True))

class Max(Function):
  @staticmethod
  def forward(ctx, input, axis=None):
    axis = [axis] if type(axis) == int else axis
    ret = reduce_op(ctx, "out = max(a,out)", "out", input, axis=axis, start="-INFINITY")
    ctx.save_for_backward(input, axis, ret)
    if axis is not None:
      ret.shape = tuple([input.shape[i] for i in range(len(input.shape)) if i not in axis])
    return ret

  @staticmethod
  def backward(ctx, grad_output):
    input, axis, ret = ctx.saved_tensors
    shape = [1 if axis is None or i in axis else input.shape[i] for i in range(len(input.shape))]
    ret2 = binary_op(ctx, "1.0*(a==b)", input, GPUBuffer(shape, ret))
    div = reduce_op(ctx, "out += a", "out+1e-10", ret2, axis=axis)
    ret3 = binary_op(ctx, "a/b", ret2, GPUBuffer(shape, div))
    return binary_op(ctx, 'a*b', ret3, GPUBuffer(shape, grad_output))

# ************* binary ops *************

@functools.lru_cache()
def get_binop_prg(thr, code, complist):
  ndims = len(complist)
  args = "".join([", int d%d" % i for i in range(ndims)]) + "".join([", int p%d" % i for i in range(ndims-1)])
  compute_idx_rets = ["\n    int idx_ret"+str(i)+" = (gid0 / "+("p%d"%i if i < ndims-1 else "1")+") % d"+str(i)+";" for i in range(ndims)]

  idx_exprs = ["0", "0"] # [idx_x, idx_y]
  for i in range(ndims):
    for j in range(2):
      if complist[i][j]:
        idx_exprs[j] = "idx_ret%d + d%d*(%s)" % (i, i, idx_exprs[j])

  return clbuild(thr, """KERNEL void binop(GLOBAL_MEM const float *x_g, GLOBAL_MEM const float *y_g, GLOBAL_MEM float *res_g""" + args + """) {
    int gid0 = get_global_id(0);"""+"".join(compute_idx_rets)+"""
    float a = x_g["""+idx_exprs[0]+"""];
    float b = y_g["""+idx_exprs[1]+"""];
    res_g[gid0] = """+code+""";\n}""", "binop")

def binary_op(ctx, code, x, y):
  n_dims = max(len(x.shape), len(y.shape))
  shape_x, shape_y = np.ones(n_dims, dtype=np.int32), np.ones(n_dims, dtype=np.int32)
  shape_x[:len(x.shape)] = np.array(x.shape, dtype=np.int32)
  shape_y[:len(y.shape)] = np.array(y.shape, dtype=np.int32)
  if not np.all((shape_x == 1) | (shape_y == 1) | (shape_x == shape_y)):
    raise Exception(f"binary op unbroadcastable shape mismatch: {x.shape} vs {y.shape}")
  shape_ret = np.maximum(shape_x, shape_y)

  dimlist, complist = [], [] # note: len(dimlist) may be less than n_dims
  def push(dim, comp):
    if len(complist) > 0 and complist[-1] == comp:
      dimlist[-1] *= dim
    elif comp != (False, False):
      dimlist.append(dim); complist.append(comp)
  for i in range(n_dims): # group together any adjacent dimensions that we can to simplify broadcasting
    push(i32(max(shape_x[i], shape_y[i])), (shape_x[i] > 1, shape_y[i] > 1))

  prg = get_binop_prg(ctx.thr, code, tuple(complist))
  ret = buffer_new(ctx, shape_ret, zero=True)
  prod_list = np.array(dimlist, dtype=i32)[-1::-1].cumprod(dtype=i32)[-1::-1] # take cumprod from back to front
  prg(x.cl, y.cl, ret.cl, *dimlist, *(prod_list[1:]),global_size=[int(prod_list[0])] if len(dimlist) > 0 else [int(1)])
  return ret

def unbroadcast(ctx, out, in_sh):
  sum_axis = [i for i in range(len(in_sh)) if in_sh[i]==1 and out.shape[i]>1] if in_sh != (1,) else None
  return reduce_op(ctx, "out += a", "out", out, sum_axis)

class Add(Function):
  @staticmethod
  def forward(ctx, x, y):
    ctx.save_for_backward(x.shape, y.shape)
    return binary_op(ctx, 'a+b', x, y)

  @staticmethod
  def backward(ctx, grad_output):
    grad_x, grad_y = grad_output, grad_output
    shape_x, shape_y = ctx.saved_tensors
    return unbroadcast(ctx, grad_x, shape_x), unbroadcast(ctx, grad_y, shape_y),

class Sub(Function):
  @staticmethod
  def forward(ctx, x, y):
    ctx.save_for_backward(x.shape, y.shape)
    return binary_op(ctx, 'a-b', x, y)

  @staticmethod
  def backward(ctx, grad_output):
    grad_x, grad_y = grad_output, unary_op(ctx, '-a', grad_output)
    shape_x, shape_y = ctx.saved_tensors
    return unbroadcast(ctx, grad_x, shape_x), unbroadcast(ctx, grad_y, shape_y),

class Mul(Function):
  @staticmethod
  def forward(ctx, x, y):
    ctx.save_for_backward(x, y)
    return binary_op(ctx, 'a*b', x, y)

  @staticmethod
  def backward(ctx, grad_output):
    x,y = ctx.saved_tensors
    grad_x = binary_op(ctx, 'a*b', y, grad_output)
    grad_y = binary_op(ctx, 'a*b', x, grad_output)
    return unbroadcast(ctx, grad_x, x.shape), unbroadcast(ctx, grad_y, y.shape),

class Pow(Function):
  @staticmethod
  def forward(ctx, x, y):
    ctx.save_for_backward(x, y)
    return binary_op(ctx, 'pow(a,b)', x, y)

  @staticmethod
  def backward(ctx, grad_output):
    x,y = ctx.saved_tensors
    grad_x = binary_op(ctx, 'a*b', grad_output,
                      binary_op(ctx, 'b * (pow((float)a, (float)(b-1.0)))', x, y))
    grad_y = binary_op(ctx, 'a*b', grad_output,
                      binary_op(ctx, 'pow(a, (float)b) * log(a);', x, y))
    return unbroadcast(ctx, grad_x, x.shape), unbroadcast(ctx, grad_y, y.shape),

# ************* movement ops *************

class Reshape(Function):
  @staticmethod
  def forward(ctx, x, shape):
    ctx.save_for_backward(x.shape)
    shape = tuple(-np.prod(x.shape) // np.prod(shape) if s == -1 else s for s in shape)
    r = GPUBuffer(shape, hostbuf=x)
    assert np.prod(x.shape) == np.prod(r.shape)
    return r

  @staticmethod
  def backward(ctx, grad_output):
    in_shape, = ctx.saved_tensors
    return GPUBuffer(in_shape, hostbuf=grad_output)

def perm_axis(ctx, inp, order):
  osize = np.array(inp.shape)[list(order)]
  ret = buffer_new(ctx, osize)
  perm = clbuild(ctx.thr, """
  KERNEL void perm(GLOBAL_MEM const float *a_g, GLOBAL_MEM float *res_g, int n_axis,
                       GLOBAL_MEM const int *shape, GLOBAL_MEM const int *order) {
    SIZE_T gid = get_global_id(0);
    int gi = gid;
    int idx = 0;
    for(int i = n_axis-1; i>-1; i--) {
      int stride = 1;
      for(int j=order[i]+1; j<n_axis; j++) stride *= shape[j];
      idx += (gi % shape[order[i]])*stride;
      gi /= shape[order[i]];
    }
    res_g[gid] = a_g[idx];
    }""", "perm")
  perm(inp.cl, ret.cl, i32(len(osize)),
    buffer_np(ctx, np.array(inp.shape, dtype=np.int32)),
    buffer_np(ctx, np.array(order, dtype=np.int32)), global_size=[int(np.prod(osize))])
  return ret

class Transpose(Function):
  @staticmethod
  def forward(ctx, x, order=(1,0)):
    ctx.save_for_backward(order)
    return perm_axis(ctx, x, order)

  @staticmethod
  def backward(ctx, grad_output):
    return perm_axis(ctx, grad_output, np.argsort(ctx.order))

# TODO: merge this with perm axis
def inner_slice(ctx, x, arg):
  shift = [y[0] for y in arg]
  oshape = [y[1]-y[0] for y in arg]
  ret = buffer_new(ctx, oshape)
  gslice = clbuild(ctx.thr, """
  KERNEL void gslice(GLOBAL_MEM const float *input, GLOBAL_MEM float *output, int prod, int n_dims,
                     GLOBAL_MEM const int *shape_x, GLOBAL_MEM const int *shape_ret,
                     GLOBAL_MEM const int *shift) {
    SIZE_T gid = get_global_id(0);
    int iptr = 0;
    int zero = 1;
    for (int dim = 0; dim < n_dims; dim++) {
      prod /= shape_ret[dim];
      int sidx = (gid / prod) % shape_ret[dim] + shift[dim];
      zero &= (sidx >= 0 && sidx < shape_x[dim]);
      iptr = (iptr * shape_x[dim]) + sidx;
    }
    output[gid] = zero ? input[iptr] : 0.0;
  }""","gslice")
  gslice(x.cl, ret.cl, i32(np.prod(ret.shape)), i32(len(ret.shape)),
    buffer_np(ctx, np.array(x.shape, dtype=np.int32)),
    buffer_np(ctx, np.array(ret.shape, dtype=np.int32)),
    buffer_np(ctx, np.array(shift, dtype=np.int32)), global_size=[int(np.prod(ret.shape))])
  return ret

class Slice(Function):
  @staticmethod
  def forward(ctx, x, arg=None):
    ctx.save_for_backward(x.shape)
    return inner_slice(ctx, x, arg)

  @staticmethod
  def backward(ctx, grad_output):
    shape, = ctx.saved_tensors
    narg = [(0-p[0], grad_output.shape[i]+(shape[i]-p[1])) for i,p in enumerate(ctx.arg)]
    return inner_slice(ctx, grad_output, narg)

# ************* processing ops *************

class Matmul(Function):
  @staticmethod
  def forward(ctx, input, weight):
    assert input.shape[-1] == weight.shape[-2]
    cnt = np.prod(input.shape[0:-2]) if len(input.shape) > 2 else 1
    isize, msize, osize = i32(input.shape[-2]), i32(input.shape[-1]), i32(weight.shape[-1])
    ret = buffer_new(ctx, list(input.shape[0:-2])+[isize, osize])

    matmul = clbuild(ctx.thr, """
     KERNEL void matmul(
       GLOBAL_MEM const float *input, GLOBAL_MEM const float *weight, GLOBAL_MEM float *res,
       int isize, int is0, int is1, int msize, int ws0, int ws1, int osize
    ) {
       SIZE_T stride = get_global_id(2);

       SIZE_T X = get_global_id(0); // isize
       SIZE_T Y = get_global_id(1); // osize

      float ret = 0.0;
      for (int x = 0; x < msize; x++) {
        ret += input[X * is0 + x * is1 + isize*msize*stride] *
          weight[Y * ws0 + x * ws1 + msize*osize*stride];
      }

      res[X * osize + Y + isize*osize*stride] = ret;
    }""", "matmul")
    ctx.save_for_backward(input, weight, matmul, cnt)

    # (isize,msize) x (msize,osize) = (isize,osize)
    matmul(input.cl, weight.cl, ret.cl, isize,
      msize, i32(1), msize, i32(1), osize, osize, global_size=[int(isize), int(osize), int(cnt)])
    return ret

  @staticmethod
  def backward(ctx, grad_output):
    input, weight, matmul, cnt = ctx.saved_tensors
    isize, msize, osize = i32(input.shape[-2]), i32(input.shape[-1]), i32(weight.shape[-1])

    grad_input = buffer_new(ctx, input.shape)
    grad_weight = buffer_new(ctx, weight.shape)

    # (isize,osize) x (msize,osize) = (isize,msize)
    matmul(grad_output.cl, weight.cl, grad_input.cl, isize,
                  osize, i32(1), osize, osize, i32(1), msize, global_size=[int(isize), int(msize), int(cnt)])

    # (isize,msize) x (isize,osize) = (msize,osize)
    matmul(
      input.cl, grad_output.cl, grad_weight.cl, msize,
      i32(1), msize, isize, i32(1), osize, osize, global_size=[int(msize), int(osize), int(cnt)])

    return grad_input, grad_weight

class Conv2D(Function):
  @staticmethod
  def forward(ctx, x, w, stride=1, groups=1):
    if type(ctx.stride) == int:
      ctx.stride = (ctx.stride, ctx.stride)
    cout,cin,H,W = w.shape
    ys,xs = ctx.stride
    bs,cin_,iy,ix = x.shape
    oy,ox = (iy-(H-ys))//ys, (ix-(W-xs))//xs
    assert cin*ctx.groups == cin_
    assert cout % ctx.groups == 0
    rcout = cout//ctx.groups

    ctx.save_for_backward(x,w)

    # output buffer
    ret = buffer_new(ctx, (bs, cout, oy, ox))

    # input  = (bs, groups, cin, iy, ix)
    # weight = (groups, rcout, cin, H, W)
    # output = (bs, groups, rcout, oy, ox)

    conv = clbuild(ctx.thr, """
    KERNEL void conv(GLOBAL_MEM const float *input, GLOBAL_MEM const float *weight, GLOBAL_MEM float *output,
      int H, int W, int groups, int rcout, int cin, int oy, int ox, int iy, int ix, int ys, int xs) {

      int B = get_global_id(0)/(groups*rcout);  // range 0-bs
      int g = (get_global_id(0)/rcout)%groups;
      int c = get_global_id(0) % rcout;

      int Y = get_global_id(1);  // range 0-oy
      int X = get_global_id(2);  // range 0-ox
      int IY = Y*ys;
      int IX = X*xs;

      float acc = 0.0;
      for (int ci = 0; ci < cin; ci++) {
        for (int y = IY; y < IY+H; y++) {
          for (int x = IX; x < IX+W; x++) {
            acc += input[B*groups*cin*iy*ix + g*cin*iy*ix + ci*iy*ix + y*ix + x] * \
              weight[g*rcout*cin*H*W + c*cin*H*W + ci*H*W + (y-IY)*W + (x-IX)];
          }
        }
      }
      output[B*groups*rcout*oy*ox + g*rcout*oy*ox + c*oy*ox + Y*ox + X] = acc;
    }""","conv")

    conv(x.cl, w.cl, ret.cl,
      i32(H), i32(W), i32(groups), i32(rcout), i32(cin),
      i32(oy), i32(ox), i32(iy), i32(ix), i32(ys), i32(xs),
      global_size=[int(bs * groups * rcout), int(oy), int(ox)])
    return ret

  @staticmethod
  def backward(ctx, grad_output):
    bs,_,oy,ox = grad_output.shape
    x, w = ctx.saved_tensors
    cout,cin,H,W = w.shape
    ys,xs = ctx.stride
    bs,cin_,iy,ix = x.shape
    oy,ox = (iy-(H-ys))//ys, (ix-(W-xs))//xs
    assert cin*ctx.groups == cin_
    assert cout % ctx.groups == 0
    rcout = cout//ctx.groups

    dx = buffer_new(ctx, (bs, cin_, iy, ix), zero=True)
    dw = buffer_new(ctx, (cout, cin, H, W))

    # tensx = (bs, groups*cin, iy, ix)
    # tensw = (groups*rcout, cin, H, W)
    # ggg = (bs, groups*rout, oy, ox)

    convw = clbuild(ctx.thr, """
    KERNEL void convw(GLOBAL_MEM const float *tensx, GLOBAL_MEM const float *ggg, GLOBAL_MEM float *dw,
      int H, int W, int groups, int rcout, int cin, int oy, int ox, int iy, int ix, int ys, int xs, int bs) {

      int g = get_global_id(0)/(rcout*cin) ; // range 0-groups
      int c = (get_global_id(0)/(cin)) %rcout; // range 0-rcout
      int ci = get_global_id(0) % cin;        // range 0-cin
      int y = get_global_id(1);  // range 0-H
      int x = get_global_id(2);  // range 0-W

      float acc = 0.0;
      for (int Y = 0; Y < oy; Y++) {
        for (int X = 0; X < ox; X++) {
          for (int B = 0; B < bs; B++) {
            acc += ggg[B*groups*rcout*oy*ox + +g*rcout*oy*ox + c*oy*ox + Y*ox + X] * \
              tensx[B*groups*cin*iy*ix + g*cin*iy*ix + ci*iy*ix + (Y*ys+y)*ix + X*xs+x];
          }
        }
      }
      dw[get_global_id(0)*H*W + y*W + x] = acc;
    }""","convw")
    convx = clbuild(ctx.thr, """
    KERNEL void convx(GLOBAL_MEM const float *tensw, GLOBAL_MEM const float *ggg, GLOBAL_MEM float *dx,
      int H, int W, int groups, int rcout, int cin, int oy, int ox, int iy, int ix, int ys, int xs, int bs) {

      int B = get_global_id(0);
      int g = get_global_id(1);
      int ci = get_global_id(2);

      for (int Y = 0; Y < oy; Y++) {
        for (int X = 0; X < ox; X++) {
          for (int y = 0; y < H; y++) {
            for (int x = 0; x < W; x++) {
              float acc = 0.0;
              for (int c = 0; c < rcout; c++) {
                acc += ggg[B*groups*rcout*oy*ox + g*rcout*oy*ox + c*oy*ox + Y*ox + X] * \
                  tensw[g*rcout*cin*H*W + c*cin*H*W + ci*H*W + y*W + x];
              }
              dx[B*groups*cin*iy*ix + g*cin*iy*ix + ci*iy*ix + (Y*ys+y)*ix + X*xs+x] += acc;
            }
          }
        }
      }
    }
    """, "convx")

    conv_args = i32(H), i32(W), i32(ctx.groups), i32(rcout), i32(cin), i32(oy), i32(ox), i32(iy), i32(ix), i32(ys), i32(xs), i32(bs)
    convw(x.cl, grad_output.cl, dw.cl, *conv_args, global_size=[int(ctx.groups * rcout * cin), int(H), int(W)])
    convx(w.cl, grad_output.cl, dx.cl, *conv_args, global_size=[int(bs), int(ctx.groups), int(cin)])
    return dx, dw
