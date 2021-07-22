from hub.core.meta.encode.base_encoder import Encoder
from hub.constants import ENCODING_DTYPE, UUID_SHIFT_AMOUNT
from hub.util.exceptions import ChunkIdEncoderError
import hub
from hub.core.storage.cachable import Cachable
import numpy as np
from uuid import uuid4
from hub.core.serialize import serialize_chunkids, deserialize_chunkids


# these constants are for accessing the data layout. see the `ChunkIdEncoder` docstring.
CHUNK_ID_INDEX = 0


class ChunkIdEncoder(Encoder, Cachable):
    """Custom compressor that allows reading of chunk IDs from a sample index without decompressing.

    Chunk IDs:
        Chunk IDs are a `ENCODING_DTYPE` value  and this class handles generating/encoding them.

    Layout:
        `_encoded` is a 2D array.

        Rows:
            The number of rows is equal to the number of chunk IDs this encoder is responsible for.

        Columns:
            The number of columns is 2.
            Each row looks like this: [chunk_id, last_index], where `last_index` is the last index that the
            chunk with `chunk_id` contains.

        Example:
            >>> enc = ChunkIdEncoder()
            >>> enc.generate_chunk_id()
            >>> enc.num_chunks
            1
            >>> enc.register_samples(10)
            >>> enc.num_samples
            10
            >>> enc.register_samples(10)
            >>> enc.num_samples
            20
            >>> enc.num_chunks
            1
            >>> enc.generate_chunk_id()
            >>> enc.register_samples(1)
            >>> enc.num_samples
            21
            >>> enc._encoded
            [[3723322941, 19],
             [1893450271, 20]]
            >>> enc[20]
            1893450271

        Best case scenario:
            The best case scenario is when all samples fit within a single chunk. This means the number of rows is 1,
            providing a O(1) lookup.

        Worst case scenario:

            The worst case scenario is when only 1 sample fits per chunk. This means the number of rows is equal to the number
            of samples, providing a O(log(N)) lookup.

        Lookup algorithm:
            To get the chunk ID for some sample index, you do a binary search over the right-most column. This will give you
            the row that corresponds to that sample index (since the right-most column is our "last index" for that chunk ID).
            Then, you get the left-most column and that is your chunk ID!
    """

    def tobytes(self) -> memoryview:
        return serialize_chunkids(hub.__version__, [self._encoded])

    @staticmethod
    def name_from_id(id: ENCODING_DTYPE) -> str:
        """Returns the hex of `id` with the "0x" prefix removed. This is the chunk's name and should be used to determine the chunk's key.
        Can convert back into `id` using `id_from_name`. You can get the `id` for a chunk using `__getitem__`."""

        return hex(id)[2:]

    @staticmethod
    def id_from_name(name: str) -> ENCODING_DTYPE:
        """Returns the 64-bit integer from the hex `name` generated by `name_from_id`."""

        return int("0x" + name, 16)

    def get_name_for_chunk(self, chunk_index: int) -> str:
        """Gets the name for the chunk at index `chunk_index`. If you need to get the name for a chunk from a sample index, instead
        use `__getitem__`, then `name_from_id`."""

        chunk_id = self._encoded[:, CHUNK_ID_INDEX][chunk_index]
        return ChunkIdEncoder.name_from_id(chunk_id)

    @classmethod
    def frombuffer(cls, buffer: bytes):
        instance = cls()
        if not buffer:
            return instance
        version, ids = deserialize_chunkids(buffer)
        if ids.nbytes:
            instance._encoded = ids
        return instance

    @property
    def num_chunks(self) -> int:
        if self.num_samples == 0:
            return 0
        return len(self._encoded)

    def generate_chunk_id(self) -> ENCODING_DTYPE:
        """Generates a random 64bit chunk ID using uuid4. Also prepares this ID to have samples registered to it.
        This method should be called once per chunk created.

        Returns:
            ENCODING_DTYPE: The random chunk ID.
        """

        id = ENCODING_DTYPE(uuid4().int >> UUID_SHIFT_AMOUNT)

        if self.num_samples == 0:
            self._encoded = np.array([[id, -1]], dtype=ENCODING_DTYPE)

        else:
            last_index = self.num_samples - 1

            new_entry = np.array(
                [[id, last_index]],
                dtype=ENCODING_DTYPE,
            )
            self._encoded = np.concatenate([self._encoded, new_entry])

        return id

    def register_samples(self, num_samples: int):
        """Registers samples to the chunk ID that was generated last with the `generate_chunk_id` method.
        This method should be called at least once per chunk created.

        Args:
            num_samples (int): The number of samples the last chunk ID should have added to it's registration.

        Raises:
            ValueError: `num_samples` should be non-negative.
            ChunkIdEncoderError: Must call `generate_chunk_id` before registering samples.
            ChunkIdEncoderError: `num_samples` can only be 0 if it is able to be a sample continuation accross chunks.
        """

        super().register_samples(None, num_samples)

    # TODO: rename this function (maybe generalize into `translate_index`?)
    def get_local_sample_index(self, global_sample_index: int) -> int:
        """Converts `global_sample_index` into a new index that is relative to the chunk the sample belongs to.

        Example:
            Given: 2 sampes in chunk 0, 2 samples in chunk 1, and 3 samples in chunk 2.
            >>> self.num_samples
            7
            >>> self.num_chunks
            3
            >>> self.get_local_sample_index(0)
            0
            >>> self.get_local_sample_index(1)
            1
            >>> self.get_local_sample_index(2)
            0
            >>> self.get_local_sample_index(3)
            1
            >>> self.get_local_sample_index(6)
            2

        Args:
            global_sample_index (int): Index of the sample relative to the containing tensor.

        Returns:
            int: local index value between 0 and the amount of samples the chunk contains - 1.
        """

        _, chunk_index = self.__getitem__(global_sample_index, return_row_index=True)  # type: ignore

        if chunk_index == 0:
            return global_sample_index

        current_entry = self._encoded[chunk_index - 1]  # type: ignore
        last_num_samples = current_entry[self.last_index_index] + 1

        return int(global_sample_index - last_num_samples)

    def _validate_incoming_item(self, _, num_samples: int):
        if num_samples < 0:
            raise ValueError(
                f"Cannot register negative num samples. Got: {num_samples}"
            )

        if self.num_samples == 0:
            raise ChunkIdEncoderError(
                "Cannot register samples because no chunk IDs exist."
            )

        if num_samples == 0 and self.num_chunks < 2:
            raise ChunkIdEncoderError(
                "Cannot register 0 num_samples (signifying a partial sample continuing the last chunk) when no last chunk exists."
            )

        # note: do not call super() method (num_samples can be 0)

    def _combine_condition(self, _) -> bool:
        return True

    def _derive_next_last_index(self, last_index: ENCODING_DTYPE, num_samples: int):
        # this operation will trigger an overflow for the first addition, so supress the warning
        np.seterr(over="ignore")
        new_last_index = last_index + ENCODING_DTYPE(num_samples)
        np.seterr(over="warn")

        return new_last_index

    def _derive_value(self, row: np.ndarray, *_) -> np.ndarray:
        return row[CHUNK_ID_INDEX]
