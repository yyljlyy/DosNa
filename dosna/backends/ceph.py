

import logging as log

import rados
import numpy as np

from .. import Backend
from ..base import BaseCluster, BasePool, BaseDataset, BaseDataChunk
from ..utils import dtype2str, shape2str, str2shape, str2dtype


class CephCluster(BaseCluster):
    """
    A Ceph Cluster that wraps LibRados.Cluster
    """
    
    def __init__(self, conffile='ceph.conf', timeout=5, **kwargs):
        super(CephCluster, self).__init__(**kwargs)
        self._cluster = rados.Rados(conffile=conffile)
        self._timeout = timeout
        
    def connect(self):
        self._cluster.connect(timeout=self._timeout)
        super(CephCluster, self).connect()
        
    def disconnect(self):
        self._cluster.shutdown()
        super(CephCluster, self).disconnect()
        
    def create_pool(self, name, open_mode='a'):
        if self.has_pool(name):
            raise Exception('Pool `%s` already exists' % name)
        self._cluster.create_pool(name)
        return self.get_pool(name, open_mode=open_mode)
    
    def has_pool(self, name):
        return self._cluster.pool_exists(name)
    
    def get_pool(self, name, open_mode='a'):
        if self.has_pool(name):
            return CephPool(self, name, open_mode=open_mode)
        raise Exception('Pool `%s` does not exist' % name)
        
    def del_pool(self, name):
        if self.has_pool(name):
            self._cluster.delete_pool(name)
        raise Exception('Pool `%s` does not exist' % name)


class CephPool(BasePool):
    """
    A Ceph Pool wraps LibRados.Pool
    """
    
    __signature__ = "DosNa Dataset"
    
    def open(self):
        if self.isopen:
            raise Exception('Pool {} is already open'.format(self.name))
        self._ioctx = self._cluster.open_ioctx(self.name)
        super(CephPool, self).open()
    
    def close(self):
        self._ioctx.close()
        super(CephPool, self).close()
        
    def create_dataset(self, name, shape=None, dtype=np.float32, fillvalue=0, 
                       data=None, chunks=None):
        
        if not ((shape is not None and dtype is not None) or data is not None):
            raise Exception('Provide `shape` and `dtype` or `data`')
        if self.has_dataset(name):
            raise Exception('Dataset `%s` already exists' % name)

        if data is not None:
            shape = data.shape
            dtype = data.dtype
        
        if chunks is None:
            chunk_size = shape
        else:
            chunk_size = chunks
        chunks_needed = (np.ceil(np.asarray(shape, float) / chunk_size)).astype(int)
        
        self._ioctx.write(name, self.__signature__)
        self._ioctx.set_xattr(name, 'shape', shape2str(shape))
        self._ioctx.set_xattr(name, 'dtype', dtype2str(dtype))
        self._ioctx.set_xattr(name, 'fillvalue', repr(fillvalue))
        self._ioctx.set_xattr(name, 'chunks', shape2str(chunks_needed))
        self._ioctx.set_xattr(name, 'chunk_size', shape2str(chunk_size))
        
        dataset = CephDataset(self, name, shape, dtype, fillvalue, 
                              chunks_needed, chunk_size)
        
        return dataset

    def get_dataset(self, name):
        if not self.has_dataset(name):
            raise Exception('Dataset `%s` does not exist' % name)
        shape = str2shape(self._ioctx.get_xattr(self.name, 'shape'))
        dtype = str2dtype(self._ioctx.get_xattr(self.name, 'dtype'))
        fillvalue = int(self._ioctx.get_xattr(self.name, 'fillvalue'))
        chunks = str2shape(self._ioctx.get_xattr(self.name, 'chunks'))
        chunk_size = str2shape(self._ioctx.get_xattr(self.name, 'chunk_size'))
        dataset = CephDataset(self, name, shape, dtype, fillvalue, 
                              chunks, chunk_size)
        return dataset
    
    def has_dataset(self, name):
        return self._ioctx.stat(name)[0] == len(self.__signature__) and \
               self._ioctx.read(name) == self.__signature__
    
    def del_dataset(self, name):
        if self.has_dataset(name):
            self._ioctx.remove_object(self.name)
        else:
            raise Exception('Dataset `%s` does not exist' % name)


class CephDataset(BaseDataset):
    """
    CephDataset wraps an instance of Rados.Object
    """
    
    @property
    def _ioctx(self):
        return self._pool._ioctx
    
    def _idx2name(self, idx):
        return '%s/%s' % (self.name, '.'.join(map(str, idx)))
    
    def create_chunk(self, idx, data=None, slices=None):
        if self.has_chunk(idx):
            raise Exception('DataChunk `{}` already exists'.format(idx))
        name = self._idx2name(idx)
        dtype = self.dtype
        shape = self.chunk_size
        fillvalue = self.fillvalue
        cdata = np.full(shape, fillvalue, dtype)
        if data is not None:
            cdata[slices] = data
        self._ioctx.write_full(name, cdata)
        return CephDataChunk(self, idx, name, shape, dtype, fillvalue)
    
    def get_chunk(self, idx):
        if self.has_chunk(idx):
            name = self._idx2name(idx)
            dtype = self.dtype
            shape = self.chunk_size
            fillvalue = self.fillvalue
            return CephDataChunk(self, idx, name, shape, dtype, fillvalue)
        return self.create_chunk(idx)
        
    def has_chunk(self, idx):
        name = self._idx2name(idx)
        return self._ioctx.stat(name)[0] == len(self.__signature__) and \
               self._ioctx.read(name) == self.__signature__
        
    def del_chunk(self, idx):
        if not self.has_chunk(idx):
            raise Exception('DataChunk `{}` does not exist'.format(idx))
        self._ioctx.remove_object(self._idx2name(idx))


class CephDataChunk(BaseDataChunk):
    
    @property
    def _ioctx(self):
        return self._dataset._ioctx
    
    def get_data(self, slices=None):
        if slices is None:
            slices = slice(None)
        n = np.prod(self.shape)
        data = np.fromstring(self.read(), dtype=self.dtype, count=n)
        return data[slices]

    def set_data(self, values, slices=None):
        if slices is None:
            slices = slice(None)
        cdata = self.get_data()
        cdata[slices] = values
        self.write_full(cdata.tobytes())
            
    def write_full(self, data):
        self._ioctx.write_full(self.name, data)

    def read(self, length=None, offset=0):
        if length is None:
            length = self.size * np.dtype(self.dtype).itemsize  # number of bytes
        return self._ioctx.read(self.name, length=length, offset=offset)
        