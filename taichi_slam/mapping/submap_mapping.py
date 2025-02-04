from taichi_slam.mapping.mapping_common import BaseMap
from .dense_tsdf import DenseTSDF
from .taichi_octomap import Octomap
import time
import numpy as np
import io
import zlib

class SubmapMapping:
    submap_collection: BaseMap
    global_map: BaseMap
    def __init__(self, submap_type=DenseTSDF, keyframe_step=20, sub_opts={}, global_opts={}):
        if submap_type == DenseTSDF:
            sdf_default_opts = {
                'map_scale': [10, 10],
                'voxel_scale': 0.05,
                'texture_enabled': False,
                'min_ray_length': 0.3,
                'max_ray_length': 3.0,
                'max_disp_particles': 1024*1024,
                'num_voxel_per_blk_axis': 10,
                'max_submap_num': 1000
            }
        elif submap_type == Octomap:
            sdf_default_opts = {
                'map_scale': [10, 10],
                'voxel_scale': 0.05,
                'texture_enabled': False,
                'min_ray_length': 0.3,
                'max_ray_length': 3.0,
                'max_disp_particles': 1024*1024,
                'K': 2,
                'max_submap_num': 1000
            }   

        sdf_default_opts.update(sub_opts)
        self.sub_opts = sdf_default_opts
        self.submaps = {}
        self.frame_count = 0
        self.keyframe_step = keyframe_step
        self.submap_type = submap_type
        self.exporting_global = False
        self.export_TSDF_xyz = None
        self.export_color = None
        self.export_x = None
        self.submap_collection = self.submap_type(**self.sub_opts)
        self.global_map = self.create_globalmap(global_opts)
        self.first_init = True
        self.set_exporting_global() # default is exporting local
        self.ego_motion_poses = {}
        self.pgo_poses = {}
        self.last_frame_id = None
        self.active_submap_frame_id = 0
        self.enable_texture = self.global_map.enable_texture
        self.post_local_to_global_callback = None
        # self.set_exporting_local() # default is exporting local

    def create_globalmap(self, global_opts={}):
        if self.submap_type == DenseTSDF:
            sdf_default_opts = {
                'map_scale': [100, 100],
                'voxel_scale': 0.05,
                'texture_enabled': False,
                'min_ray_length': 0.3,
                'max_ray_length': 3.0,
                'max_disp_particles': 1024*1024,
                'num_voxel_per_blk_axis': 10,
                'max_submap_num': 1024,
                'is_global_map': True
            }
        else:
            sdf_default_opts = {
                'map_scale': [100, 100],
                'voxel_scale': 0.05,
                'texture_enabled': False,
                'min_ray_length': 0.3,
                'max_ray_length': 3.0,
                'max_disp_particles': 1024*1024,
                'K': 2,
                'max_submap_num': 1000,
                'is_global_map': True
            }
        sdf_default_opts.update(global_opts)
        return self.submap_type(**sdf_default_opts)
    
    def set_dep_camera_intrinsic(self, K):
        self.submap_collection.set_dep_camera_intrinsic(K)
    
    def set_color_camera_intrinsic(self, K):
        self.submap_collection.set_color_camera_intrinsic(K)

    def set_exporting_global(self):
        self.exporting_global = True
        self.set_export_submap(self.global_map)

    def set_exporting_local(self):
        self.exporting_global = False
        self.set_export_submap(self.submap_collection)
       
    def set_export_submap(self, new_submap):
        self.export_color = new_submap.export_color
        if self.submap_type == DenseTSDF:
            self.export_TSDF_xyz = new_submap.export_TSDF_xyz
            self.num_TSDF_particles = new_submap.num_TSDF_particles
        else:
            self.export_x = new_submap.export_x
            self.num_export_particles = new_submap.num_export_particles

    def set_frame_poses(self, frame_poses, from_remote=False):
        s = time.time()
        self.pgo_poses.update(frame_poses)
        used_poses = {}
        for frame_id in frame_poses:
            if (self.last_frame_id is None or frame_id > self.last_frame_id) and frame_id in self.ego_motion_poses:
                self.last_frame_id = frame_id
            if frame_id in self.submaps:
                R = frame_poses[frame_id][0]
                T = frame_poses[frame_id][1]
                self.global_map.set_base_pose_submap(self.submaps[frame_id], R, T)
                used_poses[frame_id] = frame_poses[frame_id]
        # print(f"[SubmapMapping] Update frame poses from PGO cost {(time.time() - s)*1000:.1f}ms")
        #We need to broadcast the poses to remote
        if not from_remote:
            self.send_traj(used_poses)

    def create_new_submap(self, frame_id, R, T):
        print("[SubmapMapping] Create new submap ", frame_id)
        if self.first_init:
            self.first_init = False
        else:
            submap = self.submap_collection.export_submap()
            self.send_submap(submap)
            self.submap_collection.switch_to_next_submap()
            self.submap_collection.clear_last_TSDF_exporting = True
            self.local_to_global()
        submap_id = self.submap_collection.get_active_submap_id()
        self.global_map.set_base_pose_submap(submap_id, R, T)
        self.submap_collection.set_base_pose_submap(submap_id, R, T)
        self.submaps[frame_id] = submap_id
        self.pgo_poses[frame_id] = (R, T)
        self.active_submap_frame_id = frame_id

        print(f"[SubmapMapping] Created new submap on frame {frame_id}, now have {submap_id+1} submaps")
        if submap_id % 2 == 0:
            self.saveMap("/home/xuhao/output/test_map.npy")
        return self.submap_collection

    def need_create_new_submap(self, is_keyframe, R, T):
        if self.frame_count == 0:
            return True
        if not is_keyframe:
            return False
        if self.frame_count % self.keyframe_step == 0:
            return True
        return False

    def local_to_global(self):
        self.global_map.fuse_submaps(self.submap_collection)
        if self.post_local_to_global_callback is not None:
            self.post_local_to_global_callback(self.global_map)
    
    def convert_by_pgo(self, frame_id, R, T):
        self.ego_motion_poses[frame_id] = (R, T)
        if self.last_frame_id is not None:
            last_ego_R, last_ego_T = self.ego_motion_poses[self.last_frame_id]
            last_pgo_pose_R, last_pgo_pose_T = self.pgo_poses[self.last_frame_id]
            R = last_pgo_pose_R @ last_ego_R.T @ R
            T = last_pgo_pose_R @ last_ego_R.T @ (T - last_ego_T) + last_pgo_pose_T
        return R, T

    def recast_depth_to_map_by_frame(self, frame_id, is_keyframe, pose, ext, depthmap, texture):
        # print("[SubmapMapping] Recast depth to map by frame ", frame_id)
        R, T = pose
        R_ext, T_ext = ext
        R, T = self.convert_by_pgo(frame_id, R, T)
        if self.need_create_new_submap(is_keyframe, R, T):
            self.create_new_submap(frame_id, R, T)
        Rcam = R @ R_ext
        Tcam = T + R @ T_ext
        self.submap_collection.recast_depth_to_map(Rcam, Tcam, depthmap, texture)
        self.frame_count += 1

    def recast_pcl_to_map_by_frame(self, frame_id, is_keyframe, pose, ext, pcl, rgb_array):
        R, T = pose
        R, T = self.convert_by_pgo(frame_id, R, T)
        R_ext, T_ext = ext
        if self.need_create_new_submap(is_keyframe, R, T):
            #In early debug we use framecount as frameid
            self.create_new_submap(frame_id, R, T)
        Rcam = R @ R_ext
        Tcam = T + R @ T_ext
        self.submap_collection.recast_pcl_to_map(Rcam, Tcam, pcl, rgb_array)
        self.frame_count += 1

    def recast_depth_to_map(self, R, T, depthmap, texture):
        if self.need_create_new_submap(R, T):
            #In early debug we use framecount as frameid
            self.create_new_submap(self.frame_count, R, T)
        self.submap_collection.recast_depth_to_map(R, T, depthmap, texture)
        self.frame_count += 1
    
    def cvt_TSDF_to_voxels_slice(self, z):
        if self.exporting_global:
            self.global_map.cvt_TSDF_to_voxels_slice(z)
        else:
            self.submap_collection.cvt_TSDF_to_voxels_slice(z)

    def cvt_TSDF_surface_to_voxels(self):
        if len(self.submaps) > 0:
            if self.exporting_global:
                self.global_map.cvt_TSDF_surface_to_voxels()
                self.submap_collection.cvt_TSDF_surface_to_voxels_to(self.global_map.num_TSDF_particles, 
                        self.global_map.max_disp_particles, self.export_TSDF_xyz, self.export_color)
            else:
                self.submap_collection.cvt_TSDF_surface_to_voxels()
    
    #This is only for octomap now
    def cvt_occupy_to_voxels(self, level):
        if self.exporting_global:
            self.global_map.cvt_occupy_to_voxels(level)
            self.submap_collection.cvt_occupy_voxels_to(level, self.global_map.num_export_particles, 
                    self.global_map.max_disp_particles, self.export_x, self.export_color)
        else:
            self.submap_collection.cvt_occupy_to_voxels(level)
    
    def send_submap(self, submap):
        submap["frame_id"] = self.active_submap_frame_id
        submap["pose"] = self.pgo_poses[self.active_submap_frame_id]
        f = io.BytesIO()
        np.save(f, submap)
        s = time.time()
        compressed = zlib.compress(f.getbuffer(), level=1)
        self.map_send_handle(compressed)
        print(f"[SubmapMapping] Send submap with {len(f.getbuffer())/1024.0:.1f} kB, compressed {len(compressed)/1024:.1f}kB compress cost {(time.time() - s)*1000:.1f}ms")

    def send_traj(self, traj):
        f = io.BytesIO()
        np.save(f, traj)
        s = time.time()
        compressed = zlib.compress(f.getbuffer(), level=1)
        self.traj_send_handle(compressed)
        print(f"[SubmapMapping] Send traj with {len(f.getbuffer())/1024.0:.1f} kB, compressed {len(compressed)/1024:.1f}kB compress cost {(time.time() - s)*1000:.1f}ms")

    def input_remote_submap(self, buf):
        print(f"[SubmapMapping] Recv submap with {len(buf)/1024:.1f} kB")
        #decompress
        decompress = zlib.decompress(buf)
        f = io.BytesIO(decompress)
        submap = np.load(f, allow_pickle=True).item()
        idx = self.submap_collection.input_remote_submap(submap)
        self.global_map.set_base_pose_submap(idx, submap["pose"][0], submap["pose"][1])
        self.local_to_global()
        self.submaps[submap["frame_id"]] = idx

    def input_remote_traj(self, buf):
        #decompress
        decompress = zlib.decompress(buf)
        f = io.BytesIO(decompress)
        traj = np.load(f, allow_pickle=True).item()
        self.set_frame_poses(traj, True)
        print(f"[SubmapMapping] Recv traj with {len(traj)} poses {len(buf)/1024.0:.1f} kB")
    
    def saveMap(self, filename):
        self.global_map.saveMap(filename)
    
    def export_submap(self):
        self.submap_collection.export_submap()
