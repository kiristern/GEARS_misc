import torch
import numpy as np


def weighted_mse_loss(input, target, weight):
    """
    Weighted MSE implementation
    """
    sample_mean = torch.mean((input - target) ** 2, 1)
    return torch.mean(weight * sample_mean)

def loss_fct(pred, y, perts, weight=1, loss_type = 'micro', loss_mode = 'l2', gamma = 1):

        # Micro average MSE
        if loss_type == 'micro':
            mse_p = torch.nn.MSELoss()
            perts = np.array(perts)
            losses = torch.tensor(0.0, requires_grad=True).to(pred.device)
            for p in set(perts):
                pred_p = pred[np.where(perts==p)[0]]
                y_p = y[np.where(perts==p)[0]]
                if loss_mode == 'l2':
                    losses += torch.sum((pred_p - y_p)**2)/pred_p.shape[0]/pred_p.shape[1]
                elif loss_mode == 'l3':
                    losses += torch.sum((pred_p - y_p)**(2 + gamma))/pred_p.shape[0]/pred_p.shape[1]
                
            return losses/(len(set(perts)))

        else:
            # Weigh the loss for perturbations (unweighted by default)
            weights = np.ones(len(pred))
            non_ctrl_idx = np.where([('ctrl' != p) for p in perts])[0]
            weights[non_ctrl_idx] = weight
            loss = weighted_mse_loss(pred, y, torch.Tensor(weights).to(pred.device))
            return loss      