from functools import partial
import numpy as np

from structs.struct import transpose_structs, lens
from structs.numpy import shape_info, struct, Table, shape

from .transform import rtvec, matrix
from . import graph

from scipy.spatial.transform import Rotation as R


def fill_sparse(n, values, ids):
  dense = np.zeros((n, *values.shape[1:]), dtype=values.dtype)
  dense[ids] = values

  mask = np.full(n, False)
  mask[ids] = True
  return dense, mask

def fill_sparse_tile(n, values, ids, tile):
  assert tile.shape == values.shape[1:]
  dense = np.broadcast_to(np.expand_dims(tile, 0), (n, *tile.shape)).copy()
  dense[ids] = values

  mask = np.full(n, False)
  mask[ids] = True
  return dense, mask



def sparse_points(points):
  ids = np.flatnonzero(points.valid_points)
  return struct(corners=points.points[ids], ids=ids)

invalid_pose = struct(poses=np.eye(4), num_points=0, valid_poses=False)

def valid_pose(t):
  return struct(poses=t, valid_poses=True)


def extract_pose(points, board, camera):
  detections = sparse_points(points)
  poses = board.estimate_pose_points(camera, detections)

  return valid_pose(rtvec.to_matrix(poses))._extend(num_points=len(detections.ids))\
      if poses is not None else invalid_pose


def make_pose_table(point_table, board, cameras):

  poses = [[extract_pose(points, board, camera)
            for points in points_camera._sequence()]
           for points_camera, camera in zip(point_table._sequence(), cameras)]

  return make_2d_table(poses)


def make_point_table(detections, board):
  def extract_points(frame_dets):
    
    points, mask = fill_sparse(
        board.num_points, frame_dets.corners, frame_dets.ids)
    return struct(points=points, valid_points=mask)

  points = [[extract_points(d) for d in cam_dets]
            for cam_dets in detections]

  return make_2d_table(points)


def make_2d_table(items):
  rows = [Table.stack(row) for row in items]
  return Table.stack(rows)


dimensions = struct(
    camera=0,
    frame=1
)


def map_pairs(f, table, axis=0):
  n = table._prefix[axis]
  pairs = {}

  for i in range(n):
    row_i = table._index[i]
    for j in range(i + 1, n):
      pairs[(i, j)] = f(row_i, table._index[j])

  return pairs


def matching_points(points, board, cam1, cam2):
  points1, points2 = points._index[cam1], points._index[cam2]
  matching = []

  for i, j in zip(points1._sequence(0), points2._sequence(0)):
    row1, row2, ids = common_entries(i, j, 'valid_points')
    matching.append(struct(
        points1=row1.points,
        points2=row2.points,
        object_points=board.points[ids],
        ids=ids
    )
    )

  return transpose_structs(matching)


def common_entries(row1, row2, mask_key):
  valid = np.nonzero(row1[mask_key] & row2[mask_key])
  return row1._index[valid], row2._index[valid], valid[0]


def pattern_overlaps(table, axis=0):
  n = table._prefix[axis]
  overlaps = np.zeros([n, n])

  for i in range(n):
    for j in range(i + 1, n):
      row_i, row_j = table._index_select(
          i, axis=axis), table._index_select(j, axis=axis)

      has_pose = (row_i.valid_poses & row_j.valid_poses)
      weight = np.min([row_i.num_points, row_j.num_points], axis=0)
      overlaps[i, j] = overlaps[j, i] = np.sum(
          has_pose.astype(np.float32) * weight)
  return overlaps


def estimate_transform(table, i, j, axis=0):
  poses_i = table._index_select(i, axis=axis).poses
  poses_j = table._index_select(j, axis=axis).poses
  return matrix.align_transforms_robust(poses_i, poses_j)[0]


def fill_poses(pose_dict, n):
  valid_ids = sorted(pose_dict)
  pose_table = np.array([pose_dict[k] for k in valid_ids])

  values, mask = fill_sparse_tile(n, pose_table, valid_ids, np.eye(4))
  return Table.create(poses=values, valid_poses=mask)


def estimate_relative_poses(table, axis=0, hop_penalty=0.9):
  n = table._shape[axis]
  overlaps = pattern_overlaps(table, axis=axis)
  master, pairs = graph.select_pairs(overlaps, hop_penalty)

  pose_dict = {master: np.eye(4)}

  for parent, child in pairs:
    t = estimate_transform(table, parent, child, axis=axis)
    pose_dict[child] = t @ pose_dict[parent]

  return fill_poses(pose_dict, n), master



def valid_points(estimates, point_table):
  valid_poses = np.expand_dims(estimates.camera.valid_poses, 1) & np.expand_dims(
      estimates.rig.valid_poses, 0)
  return point_table.valid_points & np.expand_dims(valid_poses, valid_poses.ndim)


def valid_reprojection_error(points1, points2):
  errors, mask = reprojection_error(points1, points2)
  return errors[mask]


def reprojection_error(points1, points2):
  mask = points1.valid_points & points2.valid_points
  error = np.linalg.norm(points1.points - points2.points, axis=-1)
  error[~mask] = 0
  
  return error, mask


def inverse(table):
  return table._extend(poses=np.linalg.inv(table.poses))

def post_multiply(table, t):
  return table._extend(poses=table.poses @ t)

def pre_multiply(t, table):
  return table._extend(poses=t @ table.poses)


def can_broadcast(shape1, shape2):
  return  len(shape1) == len(shape2) and all(
      [n1 == n2 or n1 == 1 or n2 == 1 for n1, n2 in zip(shape1, shape2)])


def broadcast_to(table1, table2):
  assert can_broadcast(table1._shape, table2._shape),\
     f"broadcast_to: table shapes must broadcast {table1._shape} vs {table2._shape}"

  return table1._zipWith(lambda t1, t2: np.broadcast_to(t1, t2.shape), table2)

def multiply_tables(table1, table2):
  assert can_broadcast(table1._shape, table2._shape),\
     f"multiply_tables: table shapes must broadcast {table1._shape} vs {table2._shape}"

  return Table.create(
    poses=table1.poses @ table2.poses,
    valid_poses= table1.valid_poses & table2.valid_poses
  )

def multiply_expand(table1, dims1, table2, dims2):
  return multiply_tables(expand(table1, dims1), expand(table2, dims2))  


def expand(table, dims):
  f = partial(np.expand_dims, axis=dims)
  return table._map(f)


def expand_poses(estimates):
  return multiply_expand(estimates.camera, 1, estimates.rig, 0)  
 
def mean_robust_n(pose_table, axis=0):
  def f(poses):
    if not np.any(poses.valid_poses):
      return invalid_pose
    else:
      return valid_pose(matrix.mean_robust(poses.poses[poses.valid_poses]))

  mean_poses = [f(poses) for poses in pose_table._sequence(axis)]
  return Table.stack(mean_poses)


def relative_between(table1, table2):
  common1, common2, valid = common_entries(table1, table2, mask_key='valid_poses')
  if valid.size == 0:
    return invalid_pose
  else:
    t, _ = matrix.align_transforms_robust(common1.poses, common2.poses)
    return valid_pose(t)

def relative_between_inv(table1, table2):
  return inverse(relative_between(inverse(table1), inverse(table2)))


def relative_between_n(table1, table2, axis=0, inv=False):

  f = relative_between_inv if inv else relative_between 
  relative_poses = [f(poses1, poses2) for poses1, poses2 
    in zip(table1._sequence(axis), table2._sequence(axis))]

  return Table.stack(relative_poses)




def initialise_poses(pose_table):
    # Find relative transforms between cameras and rig poses
  camera, _ = estimate_relative_poses(pose_table, axis=0)

  # solve for the rig transforms cam @ rig = pose
  # camera_relative = multiply_tables(expand( inverse(camera), 1), pose_table)
  
  expanded = broadcast_to(expand(camera, 1), pose_table)
  rig = relative_between_n(expanded, pose_table, axis=1, inv=True)

  return struct(
    rig = rig,
    camera = camera
  )

  



def stereo_calibrate(points, board, cameras, i, j, **kwargs):
  matching = matching_points(points, board, i, j)
  return stereo_calibrate((cameras[i], cameras[j]), matching, **kwargs)