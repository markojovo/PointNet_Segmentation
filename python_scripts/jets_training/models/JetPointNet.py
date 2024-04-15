
'''
Adapted From
https://github.com/lattice-ai/pointnet/tree/master

Original Architecture From Pointnet Paper:
https://arxiv.org/pdf/1612.00593.pdf
'''


import tensorflow as tf
import numpy as np
import keras 

# =======================================================================================================================
# ============ Weird Stuff ==============================================================================================


class SaveModel(keras.callbacks.Callback):
    def on_epoch_end(self, epoch, logs={}):
        self.model.save("JetPointNet_{epoch}.hd5".format(epoch))

class CustomMaskingLayer(tf.keras.layers.Layer):
    # For masking out the inputs properly, based on points for which the last value in the point's array (it's "type") is "-1"
    def __init__(self, **kwargs):
        super(CustomMaskingLayer, self).__init__(**kwargs)
    
    def call(self, inputs):
        mask = tf.not_equal(inputs[:, :, -1], -1) # Masking
        mask = tf.cast(mask, tf.float32)
        mask = tf.expand_dims(mask, -1)
        return inputs * mask

    def compute_output_shape(self, input_shape):
        return input_shape

class OrthogonalRegularizer(tf.keras.regularizers.Regularizer):
    # Used in Tnet in PointNet for transforming everything to same space
    def __init__(self, num_features=6, l2=0.001):
        self.num_features = num_features
        self.l2 = l2
        self.I = tf.eye(num_features)

    def __call__(self, inputs):
        A = tf.reshape(inputs, (-1, self.num_features, self.num_features))
        AAT = tf.tensordot(A, A, axes=(2, 2))
        AAT = tf.reshape(AAT, (-1, self.num_features, self.num_features))
        return tf.reduce_sum(self.l2 * tf.square(AAT - self.I))

    def get_config(self):
        # Return a dictionary containing the parameters of the regularizer to allow for model serialization
        return {'num_features': self.num_features, 'l2': self.l2}
    

def rectified_TSSR_Activation(x):
    a = 0.01 # leaky ReLu style slope when negative
    b = 0.1 # sqrt(x) damping coefficient when x > 1
    
    # Adapted from https://arxiv.org/pdf/2308.04832.pdf
    # An activation function that's linear when 0 < x < 1 and (an adjusted) sqrt when x > 1,
    # behaves like leaky ReLU when x < 0.

    # 'a' is the slope coefficient for x < 0.
    # 'b' is the value to multiply by the sqrt(x) part.

    negative_condition = x < 0
    small_positive_condition = tf.logical_and(tf.greater_equal(x, 0), tf.less(x, 1))
    #large_positive_condition = x >= 1
    
    negative_part = a * x
    small_positive_part = x
    large_positive_part = tf.sign(x) * (b * tf.sqrt(tf.abs(x)) - b + 1)
    
    return tf.where(negative_condition, negative_part, 
                    tf.where(small_positive_condition, small_positive_part, large_positive_part))

def custom_sigmoid(x, a = 3.0):
    return 1 / (1 + tf.exp(-a * x))

def hard_sigmoid(x):
    return tf.keras.backend.cast(x > 0, dtype=tf.float32)

# =======================================================================================================================
# =======================================================================================================================



# =======================================================================================================================
# ============ Main Model Blocks ========================================================================================

def conv_mlp(input_tensor, filters, dropout_rate = None):
    # Apply shared MLPs which are equivalent to 1D convolutions with kernel size 1
    x = tf.keras.layers.Conv1D(filters=filters, kernel_size=1, activation='relu')(input_tensor)
    x = tf.keras.layers.BatchNormalization()(x)
    if dropout_rate is not None:
        x = tf.keras.layers.Dropout(dropout_rate)(x)
    return x

def dense_block(input_tensor, units, dropout_rate=None, regularizer=None):
    x = tf.keras.layers.Dense(units, kernel_regularizer=regularizer)(input_tensor)
    x = tf.keras.layers.BatchNormalization()(x)
    x = tf.keras.layers.Activation("relu")(x)
    if dropout_rate is not None:
        x = tf.keras.layers.Dropout(dropout_rate)(x)
    return x

def TNet(input_tensor, size, add_regularization=False):
    # size is either 6 for the first TNet or 64 for the second
    x = conv_mlp(input_tensor, 64)
    x = conv_mlp(x, 128)
    x = conv_mlp(x, 1024)
    x = tf.keras.layers.GlobalMaxPooling1D()(x)
    x = dense_block(x, 512)
    x = dense_block(x, 256)
    if add_regularization:
        reg = OrthogonalRegularizer(size)
    else:
        reg = None
    x = dense_block(x, size * size, regularizer=reg)
    x = tf.reshape(x, (-1, size, size))
    return x        


def PointNetSegmentation(num_points, num_classes):
    num_features = 6  # Number of input features per point
    '''
    Input shape per point is:
       [x (mm),
        y (mm),
        z (mm),
        minimum_of_distance_to_focused_track (mm),
        energy (MeV),
        type (-1 for masked, 0 for calorimeter cell, 1 for focused track and 2 for other track)]
    
    Note that in awk_to_npz.py, if add_tracks_as_labels == False then the labels for the tracks is "-1" (to be masked of the loss and not predicted on)

    '''

    network_size_factor = 5 # Mess around with this along with the different layer sizes 

    input_points = tf.keras.Input(shape=(num_points, num_features))

    # T-Net for input transformation
    input_tnet = TNet(input_points, num_features)
    x = tf.keras.layers.Dot(axes=(2, 1))([input_points, input_tnet])
    x = conv_mlp(x, 64)
    x = conv_mlp(x, 64)
    point_features = x

    # T-Net for feature transformation
    feature_tnet = TNet(x, 64, add_regularization=True)
    x = tf.keras.layers.Dot(axes=(2, 1))([x, feature_tnet])
    x = conv_mlp(x, 64 * network_size_factor)
    x = conv_mlp(x, 128 * network_size_factor)
    x = conv_mlp(x, 1024 * network_size_factor)

    # Get global features and expand
    global_feature = tf.keras.layers.GlobalMaxPooling1D()(x)
    global_feature_expanded = tf.keras.layers.Lambda(lambda x: tf.expand_dims(x, 1))(global_feature)
    global_feature_expanded = tf.keras.layers.Lambda(lambda x: tf.tile(x, [1, num_points, 1]))(global_feature_expanded)

    # Concatenate point features with global features
    c = tf.keras.layers.Concatenate()([point_features, global_feature_expanded])
    c = conv_mlp(c, 512 * network_size_factor)
    c = conv_mlp(c, 256 * network_size_factor)
    c = conv_mlp(c, 128 * network_size_factor, dropout_rate=0.3)

    # Extract energy from input and multiply by the segmentation output
    energy = tf.expand_dims(input_points[:, :, 4], -1)  # Assuming energy is at index 4
    segmentation_output_pre_sigmoid = tf.keras.layers.Conv1D(num_classes, kernel_size=1)(c)  # No activation yet ("sigmoid" here is a misnomer, we're using TSSR. Feel free to update)
    segmentation_output_pre_sigmoid = tf.keras.layers.Activation("sigmoid")(segmentation_output_pre_sigmoid)
    #segmentation_output_pre_sigmoid = tf.keras.layers.Activation(rectified_TSSR_Activation)(segmentation_output_pre_sigmoid) # Apply activation + adjust float back to 32 bit for training (a smarter way to do this probably exists)
    segmentation_output = tf.multiply(segmentation_output_pre_sigmoid, energy)  # Multiply by energy

    model = tf.keras.Model(inputs=input_points, outputs=segmentation_output)

    return model

# =======================================================================================================================
# =======================================================================================================================


# =======================================================================================================================
# ============ Losses ===================================================================================================

def custom_accuracy_metric(y_true, y_pred):
    # Mask to exclude certain values (e.g., -1.0)
    mask = tf.not_equal(y_true, -1.0)
    mask = tf.cast(mask, tf.float32)
    
    # Apply the mask
    y_pred_masked = tf.boolean_mask(y_pred, mask)
    y_true_masked = tf.boolean_mask(y_true, mask)
    
    # Compute the sums of the masked predictions and true values
    sum_pred_masked = tf.reduce_sum(y_pred_masked)
    sum_true_masked = tf.reduce_sum(y_true_masked)
    
    # Avoid division by zero by adding a small constant (epsilon) to the denominator
    epsilon = 1e-8
    ratio = 100.0 * sum_pred_masked / (sum_true_masked + epsilon)
    
    # Cap the result at 1000
    accuracy = tf.minimum(ratio, 1000.0)
    
    return accuracy

def masked_mse_bce_loss(y_true, y_pred, bce_weight=100.0):
    """
    Custom loss function that combines masked MSE with a built-in BCE loss for
    penalizing predicting non-zero energy for cells that should have zero energy. The
    MSE loss is normalized by the dynamic range of y_true to make it consistent between samples.

    Parameters:
    - y_true: Tensor of true energy values.
    - y_pred: Tensor of predicted energy values.
    - bce_weight: Weight for the BCE penalty.

    Returns:
    - Normalized combined loss value.
    """
    
    # Mask setup for excluding certain values from calculations
    valid_mask = tf.not_equal(y_true, -1.0)  # Excludes -1 values from the loss calculation
    valid_mask = tf.cast(valid_mask, tf.float32)
    valid_mask = tf.squeeze(valid_mask, axis=-1)  # Ensure the mask is of correct dimension
    
    # Dynamic range normalization for MSE loss
    y_true_valid = tf.boolean_mask(y_true, valid_mask)
    dynamic_range = tf.maximum(tf.reduce_max(y_true_valid) - tf.reduce_min(y_true_valid), 1e-5)  # Avoid division by zero
    
    # MSE Loss calculation with normalization
    mse_loss = tf.keras.losses.mean_squared_error(y_true, y_pred)
    mse_loss = mse_loss * valid_mask  # Apply mask
    normalized_mse_loss = tf.reduce_sum(mse_loss / dynamic_range**2) / tf.reduce_sum(valid_mask)
    
    # BCE Loss calculation for when y_true is equal to zero 
    y_true_is_zero_mask = tf.squeeze(tf.cast(tf.equal(y_true, 0.0), tf.float32), axis=-1)
    y_preds_clipped = tf.clip_by_value(y_pred, 0.001, 0.999)
    y_true_for_bce = tf.zeros_like(y_pred)
    
    bce_loss = tf.keras.losses.binary_crossentropy(y_true_for_bce, y_preds_clipped)
    bce_loss = bce_weight * bce_loss * y_true_is_zero_mask * valid_mask  # Apply masks
    normalized_bce_loss = tf.reduce_sum(bce_loss) / tf.reduce_sum(valid_mask)
    
    # Combine normalized MSE and BCE losses
    combined_loss = normalized_mse_loss + normalized_bce_loss
    
    return combined_loss

def masked_mae_loss(y_true, y_pred_outputs):
    y_pred = y_pred_outputs
    
    # Creating a mask for valid values (excluding -1.0)
    mask = tf.not_equal(y_true, -1.0)
    mask = tf.cast(mask, tf.float32)
    mask = tf.squeeze(mask, axis=-1)  # Ensures mask is correctly dimensioned
    
    # Calculate base MAE loss
    base_loss = tf.keras.losses.mean_absolute_error(y_true, y_pred)
    masked_loss = base_loss * mask  # Apply mask to loss
    
    # Dynamic range calculation for normalization
    # Filter y_true based on the mask to include only valid values
    y_true_valid = tf.boolean_mask(y_true, mask)
    dynamic_range = tf.maximum(tf.reduce_max(y_true_valid) - tf.reduce_min(y_true_valid), 1e-5)  # Avoid division by zero

    # Normalize masked_loss by dynamic range and batch size
    num_valid_values = tf.reduce_sum(mask)  # Count of valid values
    normalized_loss = tf.reduce_sum(masked_loss / dynamic_range) / num_valid_values * 1000
    
    return normalized_loss

def masked_mse_loss(y_true, y_pred):
    # Mask setup for excluding certain values from calculations
    valid_mask = tf.not_equal(y_true, -1.0)  # Excludes -1 values from the loss calculation
    valid_mask = tf.cast(valid_mask, tf.float32)
    valid_mask = tf.squeeze(valid_mask, axis=-1)  # Ensure the mask is of correct dimension
    
    # Dynamic range normalization for MSE loss
    y_true_valid = tf.boolean_mask(y_true, valid_mask)
    dynamic_range = tf.maximum(tf.reduce_max(y_true_valid) - tf.reduce_min(y_true_valid), 1e-5)  # Avoid division by zero
    
    # MSE Loss calculation with normalization
    mse_loss = tf.keras.losses.mean_squared_error(y_true, y_pred)
    mse_loss = mse_loss * valid_mask  # Apply mask
    normalized_mse_loss = tf.reduce_sum(mse_loss / dynamic_range**2) / tf.reduce_sum(valid_mask)

    return normalized_mse_loss


def masked_huber_loss(y_true, y_pred_outputs):
    delta = 1.0
    y_pred = y_pred_outputs
    
    # Creating a mask for valid (non -1) entries
    mask = tf.not_equal(y_true, -1.0)
    mask = tf.cast(mask, tf.float32)
    mask = tf.squeeze(mask, axis=-1)  # Removes the last dimension if it's 1

    # Using Huber loss from Keras, with delta parameter for the transition between squared and linear loss
    huber_loss_fn = tf.keras.losses.Huber(delta=delta, reduction=tf.keras.losses.Reduction.NONE)
    base_loss = huber_loss_fn(y_true, y_pred)

    # Apply the mask
    masked_loss = base_loss * mask

    # Normalizing the loss to the batch size
    batch_size = tf.cast(tf.shape(y_true)[0], tf.float32)
    return tf.reduce_sum(masked_loss) / tf.reduce_sum(mask) / batch_size

def masked_bce_loss(y_true, y_pred_outputs):
    y_pred = y_pred_outputs
    
    mask = tf.not_equal(y_true, -1.0)
    mask = tf.cast(mask, tf.float32)
    mask = tf.squeeze(mask, axis=-1)  # Removes the last dimension if it's 1
    base_loss = tf.keras.losses.binary_crossentropy(y_true, y_pred, from_logits=False) 
    masked_loss = base_loss * mask

    batch_size = tf.cast(tf.shape(y_true)[0], tf.float32)
    return tf.reduce_sum(masked_loss) / tf.reduce_sum(mask) / batch_size # This might be kinda dumb, might be able to not use reduce_sum and avoid having to manually get batch_size



# =======================================================================================================================
# =======================================================================================================================
